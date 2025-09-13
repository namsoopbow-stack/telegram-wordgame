# bot.py — ĐỐI CHỮ 2 TỪ (VN) — Webhook FastAPI + PTB 21.x
# ✔ 2 từ; ✔ nối chữ (từ2 -> từ1 kế); ✔ kiểm tra nghĩa OFFLINE→ONLINE(as-is); ✔ cache
# ✔ /newgame + /join + auto start 60s; ✔ nhắc 30s; ✔ /ketthuc; ✔ /iu đặc biệt

import os, re, json, time, random, asyncio
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, AIORateLimiter
)
from unidecode import unidecode

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN")

ROUND_SECONDS    = int(os.getenv("ROUND_SECONDS", "60"))      # thời gian 1 lượt
AUTO_COUNTDOWN   = int(os.getenv("AUTO_COUNTDOWN", "60"))     # đếm ngược sảnh
DICT_FILES       = [p.strip() for p in os.getenv("DICT_FILES", "dict_vi.txt,slang_vi.txt").split(",") if p.strip()]
DICT_CACHE       = os.getenv("DICT_CACHE", "valid_cache.json")
DICT_CACHE_TTL   = int(os.getenv("DICT_CACHE_TTL", str(24*3600)))  # 24h
SPECIAL_PINGER   = os.getenv("SPECIAL_PINGER", "@yhck2").lower()
SPECIAL_TARGET   = os.getenv("SPECIAL_TARGET", "@xiaoc6789").lower()

# =========================
# TEXTS
# =========================
HELP_TEXT = (
    "🎲 Luật **ĐỐI CHỮ 2 TỪ**:\n"
    "• Mỗi đáp án phải là **cụm 2 từ** (vd: `cá heo`).\n"
    "• Cụm kế tiếp phải **bắt đầu bằng từ thứ 2** của cụm trước (vd: `heo …`).\n"
    "• Cụm phải **có nghĩa**: tra OFFLINE trước; nếu không có, tra ONLINE Wiktionary (y nguyên bạn gõ).\n"
    "• Hết giờ hoặc sai luật → kết thúc.\n\n"
    "Lệnh: /newgame mở sảnh, /join tham gia, /ketthuc dừng ván, /iu (đặc biệt).\n"
    f"Mỗi lượt {ROUND_SECONDS}s, nhắc ở 30s."
)

REMINDERS_30S = [
    "Nhanh lên nào! Thời gian không chờ đợi ai đâu!",
    "Chậm thế? Có đoán nổi không vậy!",
    "IQ chỉ tới đây thôi à? Động não lẹ lên!",
    "Đứng hình 5s à? Đoán đi chứ!",
    "Vẫn chưa ra? Đúng là não 🐷 mà!",
    "Hít thở sâu rồi trả lời nhanh nào!",
    "Gợi ý: nhớ **cụm 2 từ** nhé!",
    "Đang ngủ gật hả? Tỉnh đi!",
    "Bấm nhanh hộ cái, sắp hết giờ!",
    "Không trả lời là mất lượt đấy!",
]

TIMEOUT_TEXT = "⏰ Hết giờ! Không ai trả lời đúng. Kết thúc ván."
WRONG_FMT = [
    "❌ Sai luật: phải **cụm 2 từ**.",
    "❌ Không hợp lệ. Đọc lại luật đi nè.",
    "❌ Trật lất rồi; nhớ 2 từ và nối đúng kìa!",
    "❌ Cụm không có nghĩa hoặc dùng rồi.",
]

# =========================
# Utility
# =========================
def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def is_two_word_phrase(s: str) -> bool:
    parts = norm(s).split()
    return len(parts) == 2 and all(len(p) >= 1 for p in parts)

# =========================
# VALIDATOR: OFFLINE → ONLINE(as-is) + CACHE
# =========================
try:
    import aiohttp
except ImportError:
    aiohttp = None

VN_WIKI_API = "https://vi.wiktionary.org/w/api.php"

class WordValidator:
    """
    1) OFFLINE: so cụm có dấu và bản không dấu (từ điển nội bộ).
    2) ONLINE as-is: gọi Wiktionary VI với đúng chuỗi người chơi gõ (không sửa dấu).
    3) Cache RAM + file để không tra lại.
    """
    def __init__(self, dict_paths, cache_file: str, cache_ttl_sec: int):
        if aiohttp is None:
            raise RuntimeError("Thiếu aiohttp. Thêm 'aiohttp==3.9.*' vào requirements.txt")
        if isinstance(dict_paths, str):
            dict_paths = [dict_paths]
        self.dict_paths = [p for p in dict_paths if p]
        self.cache_file = Path(cache_file)
        self.cache_ttl = cache_ttl_sec

        self._offline_with_acc: Set[str] = set()
        self._offline_no_acc:  Set[str] = set()
        self._load_offline()

        self._cache: Dict[str, Tuple[bool, float]] = {}
        self._load_cache()

        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    def _noacc(self, s: str) -> str:
        return norm(unidecode(s or ""))

    def _load_offline(self):
        for p in self.dict_paths:
            fp = Path(p)
            if not fp.exists(): 
                continue
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    w = norm(line)
                    if not w: 
                        continue
                    self._offline_with_acc.add(w)
                    self._offline_no_acc.add(self._noacc(w))

    def _hit_offline(self, phrase: str) -> bool:
        k = norm(phrase)
        if k in self._offline_with_acc:
            return True
        if self._noacc(k) in self._offline_no_acc:
            return True
        return False

    def _load_cache(self):
        if not self.cache_file.exists():
            return
        try:
            data = json.loads(self.cache_file.read_text("utf-8"))
            now = time.time()
            for k, item in data.items():
                v = bool(item.get("v"))
                ts = float(item.get("ts", 0))
                if now - ts <= self.cache_ttl:
                    self._cache[k] = (v, ts)
        except Exception:
            pass

    def _save_cache(self):
        try:
            now = time.time()
            ser = {k: {"v": v, "ts": ts} for k, (v, ts) in self._cache.items() if now - ts <= self.cache_ttl}
            self.cache_file.write_text(json.dumps(ser, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    def _cache_get(self, key: str):
        item = self._cache.get(key)
        if not item:
            return None
        v, ts = item
        if time.time() - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return v

    def _cache_put(self, key: str, val: bool):
        self._cache[key] = (bool(val), time.time())

    async def _session_get(self):
        async with self._lock:
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
            return self._session

    async def _wiktionary_ok_as_is(self, typed_phrase: str) -> bool:
        title = norm(typed_phrase)
        if not title:
            return False
        params = {
            "action": "parse",
            "format": "json",
            "redirects": 1,
            "prop": "sections",
            "page": title
        }
        try:
            sess = await self._session_get()
            async with sess.get(VN_WIKI_API, params=params) as r:
                if r.status != 200:
                    return False
                data = await r.json()
        except Exception:
            return False

        secs = (data or {}).get("parse", {}).get("sections", [])
        for sec in secs:
            line = (sec.get("line") or "").lower()
            anchor = (sec.get("anchor") or "").lower()
            if "tiếng việt" in line or "tiếng_việt" in anchor:
                return True
        return False

    async def is_valid(self, phrase: str) -> bool:
        key = norm(phrase)
        if not key:
            return False

        hit = self._cache_get(key)
        if hit is not None:
            return hit

        if self._hit_offline(key):
            self._cache_put(key, True)
            return True

        ok = await self._wiktionary_ok_as_is(phrase)  # as-is (có/không dấu đều bê nguyên)
        self._cache_put(key, ok)
        return ok

    async def aclose(self):
        if self._session:
            await self._session.close()
        self._save_cache()

VALIDATOR = WordValidator(DICT_FILES, DICT_CACHE, DICT_CACHE_TTL)

# =========================
# GAME STATE
# =========================
class Game:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.lobby = False
        self.players: Set[int] = set()
        self.countdown_job = None

        self.running = False
        self.required_prefix: Optional[str] = None
        self.used: Set[str] = set()
        self.turn_job = None
        self.half_job = None

        self.pve_user_id: Optional[int] = None
        self.starter: Optional[str] = None

GAMES: Dict[int, Game] = {}
def get_game(cid: int) -> Game:
    if cid not in GAMES: GAMES[cid] = Game(cid)
    return GAMES[cid]

# =========================
# OFFLINE BOT MOVE
# =========================
def offline_candidates_starting_with(prefix: str) -> List[str]:
    # lấy từ trong offline (có dấu), đủ 2 từ và từ 1 == prefix
    cands = []
    # dùng EXACT offline set (có dấu) là đủ để bot đối chữ “đẹp mắt”
    for w in VALIDATOR._offline_with_acc:
        parts = w.split()
        if len(parts) == 2 and parts[0] == prefix:
            cands.append(w)
    return cands

def random_offline_two_word() -> Optional[str]:
    pool = [w for w in VALIDATOR._offline_with_acc if len(w.split()) == 2]
    return random.choice(pool) if pool else None

# =========================
# HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("Hãy thêm bot vào nhóm để chơi nhé.")
        return

    g = get_game(chat.id)
    # reset
    if g.countdown_job: g.countdown_job.schedule_removal()
    if g.turn_job: g.turn_job.schedule_removal()
    if g.half_job: g.half_job.schedule_removal()

    g.lobby = True
    g.players = set()
    g.running = False
    g.used.clear()
    g.required_prefix = None
    g.starter = None
    g.pve_user_id = None

    await update.effective_message.reply_text(
        f"🎮 **Mở sảnh**! Gõ /join để tham gia. Tự bắt đầu sau {AUTO_COUNTDOWN}s.",
        parse_mode=ParseMode.MARKDOWN
    )
    # countdown & nhắc nửa đường
    g.countdown_job = context.job_queue.run_once(lambda c: start_game_job(c, chat.id), AUTO_COUNTDOWN)
    context.job_queue.run_once(lambda c: lobby_remind(c, chat.id), AUTO_COUNTDOWN//2)

def lobby_remind(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    app: Application = context.application
    asyncio.create_task(app.bot.send_message(chat_id, f"⏳ Còn {AUTO_COUNTDOWN//2}s, /join nhanh nào!"))

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(update.effective_chat.id)
    if not g.lobby:
        await update.effective_message.reply_text("Chưa mở sảnh. Gõ /newgame trước nhé.")
        return
    uid = update.effective_user.id
    if uid in g.players:
        await update.effective_message.reply_text("Bạn đã tham gia rồi.")
        return
    g.players.add(uid)
    await update.effective_message.reply_html(f"✅ {update.effective_user.mention_html()} đã tham gia!")

def start_game_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    asyncio.create_task(_start_game(context, chat_id))

async def _start_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    app: Application = context.application
    g = get_game(chat_id)
    g.lobby = False
    n = len(g.players)
    if n == 0:
        await app.bot.send_message(chat_id, "⛔ Không ai tham gia. Huỷ ván.")
        return

    g.running = True
    g.used.clear()
    g.required_prefix = None

    # chọn từ mở màn từ OFFLINE 2 từ
    starter = random_offline_two_word()
    if not starter:
        await app.bot.send_message(chat_id, "⚠️ Không có từ mở màn trong từ điển OFFLINE. Thêm từ đi bạn nhé.")
        g.running = False
        return
    g.starter = starter
    g.used.add(starter)
    p1, p2 = starter.split()
    g.required_prefix = p2

    if n == 1:
        g.pve_user_id = list(g.players)[0]
        await app.bot.send_message(
            chat_id,
            f"👤 Chỉ 1 người → chơi với BOT.\n🎯 Mở màn: *{starter}*\n"
            f"Bạn phải bắt đầu bằng: **{g.required_prefix}**",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await app.bot.send_message(
            chat_id,
            f"👥 {n} người tham gia. BOT trọng tài.\n🎯 Mở màn: *{starter}*\n"
            f"Tiếp theo phải bắt đầu bằng: **{g.required_prefix}**",
            parse_mode=ParseMode.MARKDOWN
        )

    schedule_turn_timers(context, chat_id)

def schedule_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    if g.turn_job: g.turn_job.schedule_removal()
    if g.half_job: g.half_job.schedule_removal()
    g.half_job = context.job_queue.run_once(lambda c: half_warn(c, chat_id), ROUND_SECONDS//2)
    g.turn_job = context.job_queue.run_once(lambda c: timeup(c, chat_id), ROUND_SECONDS)

def half_warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    app: Application = context.application
    text = random.choice(REMINDERS_30S)
    asyncio.create_task(app.bot.send_message(chat_id, "⚠️ " + text))

def timeup(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    asyncio.create_task(_timeup(context, chat_id))

async def _timeup(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    app: Application = context.application
    g = get_game(chat_id)
    if not g.running: return
    g.running = False
    await app.bot.send_message(chat_id, TIMEOUT_TEXT)

async def cmd_ketthuc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(update.effective_chat.id)
    g.lobby = False
    g.running = False
    if g.countdown_job: g.countdown_job.schedule_removal()
    if g.turn_job: g.turn_job.schedule_removal()
    if g.half_job: g.half_job.schedule_removal()
    await update.effective_message.reply_text("🛑 Đã dừng ván.")

# ============ TEXT (đáp án) ============
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    g = get_game(chat.id)
    if not g.running:
        return

    msg: Message = update.effective_message
    raw = msg.text or ""
    if not is_two_word_phrase(raw):
        await msg.reply_text(random.choice(WRONG_FMT), parse_mode=ParseMode.MARKDOWN)
        return

    k = norm(raw)
    t1, t2 = k.split()

    # kiểm tra nối chữ
    if g.required_prefix and t1 != g.required_prefix:
        await msg.reply_text(f"❌ Sai nối chữ: phải bắt đầu bằng **{g.required_prefix}**", parse_mode=ParseMode.MARKDOWN)
        return

    # chưa dùng trong ván
    if k in g.used:
        await msg.reply_text("⚠️ Cụm này dùng rồi, thử cụm khác nhé.")
        return

    # kiểm tra nghĩa OFFLINE→ONLINE(as-is)
    ok = await VALIDATOR.is_valid(raw)  # dùng raw (as-is)
    if not ok:
        await msg.reply_text("❌ Cụm không có nghĩa (không tìm thấy).", parse_mode=ParseMode.MARKDOWN)
        return

    # chấp nhận
    g.used.add(k)
    g.required_prefix = t2
    await msg.reply_text(
        f"✅ Hợp lệ.\n👉 Tiếp theo bắt đầu bằng: **{g.required_prefix}**",
        parse_mode=ParseMode.MARKDOWN
    )
    schedule_turn_timers(context, chat.id)

    # PvE: nếu chỉ 1 người — để BOT đối lại
    if g.pve_user_id and update.effective_user.id == g.pve_user_id:
        await asyncio.sleep(1.0)
        await bot_play(chat.id, context)

async def bot_play(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    app: Application = context.application
    g = get_game(chat_id)
    if not g.running: return
    prefix = g.required_prefix
    cands = offline_candidates_starting_with(prefix)
    random.shuffle(cands)
    move = None
    for w in cands:
        if norm(w) not in g.used:
            move = w
            break
    if not move:
        await app.bot.send_message(chat_id, "🤖 BOT chịu thua. Bạn thắng!")
        g.running = False
        return
    g.used.add(norm(move))
    _, t2 = norm(move).split()
    g.required_prefix = t2
    await app.bot.send_message(
        chat_id,
        f"🤖 BOT: *{move}*\n👉 Tiếp theo bắt đầu bằng: **{g.required_prefix}**",
        parse_mode=ParseMode.MARKDOWN
    )
    schedule_turn_timers(context, chat_id)

# ============ /iu đặc biệt ============
async def cmd_iu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = ("@" + (user.username or "")).lower()
    if uname != SPECIAL_PINGER:
        await update.effective_message.reply_text("Chức năng này chỉ dành cho người đặc biệt 😉")
        return
    txt = "Yêu Em Thiệu 🥰 Làm Người Yêu Anh Nhé !!!"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Đồng ý 💘", callback_data="iu_yes"),
         InlineKeyboardButton("Không 😶", callback_data="iu_no")]
    ])
    await update.effective_message.reply_text(txt, reply_markup=kb)

async def on_iu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    uname = ("@" + (user.username or "")).lower()
    if uname == SPECIAL_TARGET:
        await q.edit_message_text("Em đồng ý !! Yêu Anh 🥰")
    else:
        await q.edit_message_text("Thiệu ơi !! Yêu Anh Nam Đii")

# =========================
# BUILD APP + WEBHOOK FASTAPI
# =========================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("ketthuc", cmd_ketthuc))
    app.add_handler(CommandHandler("iu", cmd_iu))
    app.add_handler(CallbackQueryHandler(on_iu_click, pattern=r"^iu_(yes|no)$"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    async def _on_shutdown(_: Application):
        await VALIDATOR.aclose()
    app.post_shutdown = _on_shutdown
    return app

app = FastAPI()
tg_app = build_app()

@app.on_event("startup")
async def _startup():
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def _shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.get("/")
async def root():
    return {"status": "ok", "game": "doi-chu-2-tu"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
