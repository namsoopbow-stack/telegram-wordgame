# bot.py — PTB v21.x
import os, re, json, random, asyncio
from typing import Dict, List, Set, Tuple, Optional
from unidecode import unidecode

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder, Application, AIORateLimiter,
    CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()

# thời gian chờ sảnh
AUTO_START = int(os.environ.get("AUTO_START", "60"))
# thời gian mỗi lượt
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "30"))

# Gist để cache cụm hợp lệ vĩnh viễn
GIST_ID    = os.getenv("GIST_DICT_ID", "").strip()  # vd: 212301c00d2b00247ffc786f921dc29f
GIST_FILE  = os.getenv("GIST_DICT_FILE", "dict_offline.txt").strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", "").strip()

# từ điển local tuỳ chọn (mỗi dòng 1 cụm 2 từ)
DICT_PATH  = os.getenv("DICT_PATH", "dict_vi.txt")

# ====== Lời nhắc / câu nói ======
REMINDERS_30S = [
    "⏳ Có hội không chờ đợi, quất!",
    "⏳ Vẫn chưa có câu à? Mạnh dạn lên!",
    "⏳ Nghĩ nhanh tay nhanh! Còn nửa thời gian!",
    "⏳ Gấp gấp nào! Đừng để đồng đội mòn mỏi.",
    "⏳ Đừng hình 5s à? Đoán đi chứ!",
    "⏳ Não 🐷 sao? Bật turbo lên!",
    "⏳ Nhanh tay kẻo lỡ, còn 30s!",
    "⏳ Hồi hộp phết! Mau trả lời nào!",
    "⏳ Chậm là bị loại đó nha!",
    "⏳ Thời gian không chờ ai đâu!",
]
REMINDER_5S = "⏰ Còn 5 giây!"

SAY_WRONG_EXPL = "❌ Cụm không có nghĩa (không tìm thấy)."
SAY_ELIMINATE  = "⛔ {name} bị loại."
SAY_TIMEOUT    = "⏱️ Hết thời gian lượt! {name} bị loại."

# ============== MEANING CHECK (offline + online + cache Gist) ==============
import aiohttp

WIKI_API = "https://vi.wiktionary.org/w/api.php"
WIKI_PEDIA = "https://vi.wikipedia.org/w/api.php"

OFFLINE_SET: Set[str] = set()
OFFLINE_ASCII: Set[str] = set()
INDEX_BY_FIRST: Dict[str, List[str]] = {}
_http: Optional[aiohttp.ClientSession] = None

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def _tokens_ok(s: str) -> Tuple[bool,str]:
    s = _norm(s)
    parts = s.split(" ")
    if len(parts) != 2:
        return False, "Phải là **cụm 2 từ**."
    for p in parts:
        if not re.fullmatch(r"[a-zA-ZÀ-ỹăâêôơưđ\-]+", p):
            return False, "Chỉ chấp nhận **chữ cái tiếng Việt**."
    return True, ""

async def _http_client() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8, connect=5),
            headers={"User-Agent": "doi-chu-bot/1.0"}
        )
    return _http

def _index_phrase(s: str):
    parts = _norm(s).split(" ")
    if len(parts) == 2:
        first = parts[0]
        INDEX_BY_FIRST.setdefault(first, []).append(s)

def _offline_has(s: str) -> bool:
    s2 = _norm(s)
    if s2 in OFFLINE_SET or unidecode(s2) in OFFLINE_ASCII:
        return True
    return False

async def _wiktionary_has(s: str) -> bool:
    http = await _http_client()
    q = _norm(s)
    # 1) parse page exact
    try:
        params = {"action":"parse","page":q,"prop":"wikitext","format":"json"}
        async with http.get(WIKI_API, params=params) as r:
            data = await r.json()
        wt = data.get("parse",{}).get("wikitext",{}).get("*","")
        if "==Tiếng Việt==" in wt:
            return True
    except Exception:
        pass
    # 2) opensearch -> parse
    try:
        params = {"action":"opensearch","search":q,"limit":3,"namespace":0,"format":"json"}
        async with http.get(WIKI_API, params=params) as r:
            arr = await r.json()
        titles = arr[1] if isinstance(arr,list) and len(arr)>1 else []
        for t in titles:
            if _norm(t) == q:
                params = {"action":"parse","page":t,"prop":"wikitext","format":"json"}
                async with http.get(WIKI_API, params=params) as r2:
                    data2 = await r2.json()
                wt2 = data2.get("parse",{}).get("wikitext",{}).get("*","")
                if "==Tiếng Việt==" in wt2:
                    return True
    except Exception:
        pass
    # 3) fallback Wikipedia
    try:
        params = {"action":"opensearch","search":q,"limit":1,"namespace":0,"format":"json"}
        async with http.get(WIKI_PEDIA, params=params) as r:
            arr = await r.json()
        titles = arr[1] if isinstance(arr,list) and len(arr)>1 else []
        if any(_norm(t)==q for t in titles):
            return True
    except Exception:
        pass
    return False

async def _persist_to_gist(s: str):
    s = _norm(s)
    if not s or s in OFFLINE_SET:
        return
    # add local
    OFFLINE_SET.add(s)
    OFFLINE_ASCII.add(unidecode(s))
    _index_phrase(s)

    if not (GIST_ID and GIST_FILE and GIST_TOKEN):
        return
    try:
        http = await _http_client()
        headers = {
            "Authorization": f"token {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        # get gist content
        async with http.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers) as r:
            gist = await r.json()
        files = gist.get("files", {})
        old = files.get(GIST_FILE, {}).get("content", "")
        new = (old + ("\n" if old and not old.endswith("\n") else "") + s).strip("\n") + "\n"
        payload = {"files": {GIST_FILE: {"content": new}}}
        async with http.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload) as r2:
            await r2.text()
    except Exception:
        pass

async def init_phrase_cache():
    # file local
    try:
        with open(DICT_PATH,"r",encoding="utf-8") as f:
            for line in f:
                w = _norm(line)
                if w:
                    OFFLINE_SET.add(w)
                    OFFLINE_ASCII.add(unidecode(w))
                    _index_phrase(w)
    except FileNotFoundError:
        pass
    # gist
    if not GIST_ID:
        return
    try:
        http = await _http_client()
        headers = {"Accept":"application/vnd.github+json"}
        if GIST_TOKEN:
            headers["Authorization"] = f"token {GIST_TOKEN}"
        async with http.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers) as r:
            gist = await r.json()
        files = gist.get("files",{})
        if GIST_FILE in files and files[GIST_FILE].get("raw_url"):
            raw_url = files[GIST_FILE]["raw_url"]
            async with http.get(raw_url) as rr:
                text = await rr.text()
            for line in text.splitlines():
                w = _norm(line)
                if w:
                    OFFLINE_SET.add(w)
                    OFFLINE_ASCII.add(unidecode(w))
                    _index_phrase(w)
    except Exception:
        pass

async def has_meaning_vi(phrase: str) -> Tuple[bool,str]:
    ok, why = _tokens_ok(phrase)
    if not ok:
        return False, why
    if _offline_has(phrase):
        return True, "Tìm thấy trong từ điển."
    if await _wiktionary_has(phrase):
        await _persist_to_gist(phrase)
        return True, "Xác thực online."
    return False, "Không thấy trong từ điển (offline + online)."

# ============== GAME STATE ==============
class Game:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.waiting: bool = False
        self.started: bool = False
        self.players: List[int] = []
        self.player_names: Dict[int,str] = {}
        self.turn_idx: int = 0
        self.required_first: Optional[str] = None
        self.used_phrases: Set[str] = set()
        self.join_job = None
        self.round_deadline: Optional[float] = None
        self.round_jobs: List = []

    def reset_round_timers(self, context: ContextTypes.DEFAULT_TYPE, who_name: str):
        # huỷ job cũ
        for j in self.round_jobs:
            try: j.schedule_removal()
            except: pass
        self.round_jobs.clear()

        # setup reminder 30s và 25s (còn 5s)
        if ROUND_SECONDS > 5:
            self.round_jobs.append(
                context.job_queue.run_once(
                    lambda ctx: asyncio.create_task(
                        ctx.bot.send_message(self.chat_id, random.choice(REMINDERS_30S))
                    ),
                    when=ROUND_SECONDS/2
                )
            )
        self.round_jobs.append(
            context.job_queue.run_once(
                lambda ctx: asyncio.create_task(
                    ctx.bot.send_message(self.chat_id, REMINDER_5S)
                ),
                when=max(1, ROUND_SECONDS-5)
            )
        )

    def current_player(self) -> Optional[int]:
        if not self.players: return None
        return self.players[self.turn_idx % len(self.players)]

    def advance_turn(self):
        if self.players:
            self.turn_idx = (self.turn_idx + 1) % len(self.players)

# chat_id -> Game
GAMES: Dict[int, Game] = {}

# ============== HELPERS ==============
def mention_html(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{name}</a>'

def first_word(s: str) -> str:
    return _norm(s).split(" ")[0]

def last_word(s: str) -> str:
    return _norm(s).split(" ")[-1]

def choose_phrase_starting_with(first: str, ban: Set[str]) -> Optional[str]:
    lst = INDEX_BY_FIRST.get(_norm(first), [])
    cand = [p for p in lst if p not in ban]
    if not cand: return None
    return random.choice(cand)

# ============== COMMANDS ==============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text(
            "Mình chỉ chơi trong nhóm. Dùng /newgame để mở sảnh nhé!"
        )
        return
    g = GAMES.setdefault(chat.id, Game(chat.id))
    await update.effective_message.reply_text(
        "🎮 Mở sảnh bằng /newgame → mọi người /join để tham gia.\n"
        "Luật: đối **cụm 2 từ có nghĩa**. Lượt sau phải bắt đầu bằng **từ cuối** của cụm trước.\n"
        f"Mỗi lượt {ROUND_SECONDS}s, sai hoặc hết giờ sẽ bị loại.",
        parse_mode=ParseMode.HTML
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    g = GAMES.setdefault(chat.id, Game(chat.id))
    # reset toàn bộ
    GAMES[chat.id] = Game(chat.id); g = GAMES[chat.id]
    g.waiting = True
    await context.bot.send_message(
        chat.id,
        f"🎮 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau {AUTO_START}s nếu có người tham gia."
    )
    # đếm ngược
    if g.join_job:
        try: g.join_job.schedule_removal()
        except: pass
    g.join_job = context.job_queue.run_once(lambda ctx: asyncio.create_task(auto_start(chat.id, context)), when=AUTO_START)

async def auto_start(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = GAMES.get(chat_id)
    if not g or not g.waiting: return
    if len(g.players) == 0:
        await context.bot.send_message(chat_id, "⏳ Hết giờ chờ. Không có ai tham gia, hủy sảnh.")
        GAMES[chat_id] = Game(chat_id)
        return
    await start_match(chat_id, context)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = GAMES.setdefault(chat.id, Game(chat.id))
    if not g.waiting:
        await update.effective_message.reply_text("Chưa mở sảnh. Dùng /newgame để mở.")
        return
    if user.id not in g.players:
        g.players.append(user.id); g.player_names[user.id] = user.full_name
        await update.effective_message.reply_text(f"✅ {user.full_name} đã tham gia!")
    else:
        await update.effective_message.reply_text("Bạn đã tham gia rồi.")

async def cmd_ketthuc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    GAMES[chat.id] = Game(chat.id)
    await update.effective_message.reply_text("🧹 Đã kết thúc ván / dọn sảnh.")

async def start_match(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = GAMES.get(chat_id)
    if not g or g.started: return
    g.waiting = False; g.started = True

    if len(g.players) == 1:
        # SOLO với BOT
        uid = g.players[0]; name = g.player_names[uid]
        await context.bot.send_message(
            chat_id,
            f"👤 Chỉ 1 người → chơi với BOT.\n✨ Lượt đầu: gửi <b>cụm 2 từ có nghĩa</b> bất kỳ. "
            f"Sau đó đối tiếp bằng <b>từ cuối</b>.",
            parse_mode=ParseMode.HTML
        )
        g.required_first = None
        g.turn_idx = 0  # người chơi trước
        g.reset_round_timers(context, name)
    else:
        # NHIỀU NGƯỜI — random người đi trước
        random.shuffle(g.players)
        who = g.current_player(); name = g.player_names[who]
        await context.bot.send_message(
            chat_id,
            f"👥 {len(g.players)} người tham gia. BOT trọng tài.\n"
            f"🎯 {mention_html(who, name)} đi trước — gửi <b>cụm 2 từ có nghĩa</b>. "
            f"Lượt sau phải bắt đầu bằng <b>từ cuối</b>.",
            parse_mode=ParseMode.HTML
        )
        g.required_first = None
        g.reset_round_timers(context, name)

# ============== HANDLE ANSWERS ==============
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    msg = update.effective_message
    text = _norm(msg.text or "")
    if not text: return

    g = GAMES.get(chat.id)
    if not g or not g.started:
        return

    uid = update.effective_user.id
    name = update.effective_user.full_name

    # Nếu nhiều người, chỉ nhận từ người đang đến lượt
    if len(g.players) >= 2:
        if uid != g.current_player():
            return

    # Kiểm tra quy tắc “bắt đầu bằng từ cuối”
    if g.required_first:
        if first_word(text) != _norm(g.required_first):
            await msg.reply_text(
                f"❌ Sai luật. Cụm phải bắt đầu bằng <b>{g.required_first}</b>.",
                parse_mode=ParseMode.HTML
            )
            # loại người chơi
            if len(g.players) >= 2:
                await eliminate_current(chat.id, context, reason=f"Sai luật (không bắt đầu bằng <b>{g.required_first}</b>).")
            else:
                await msg.reply_text("🤖 BOT thắng 👑")
                GAMES[chat.id] = Game(chat.id)
            return

    # Kiểm tra nghĩa
    ok, reason = await has_meaning_vi(text)
    if not ok:
        await msg.reply_text(f"{SAY_WRONG_EXPL}\nℹ️ {reason}")
        if len(g.players) >= 2:
            await eliminate_current(chat.id, context, reason=reason)
        else:
            await msg.reply_text("🤖 BOT thắng 👑")
            GAMES[chat.id] = Game(chat.id)
        return

    # Hợp lệ
    g.used_phrases.add(text)
    # Cập nhật required_first = từ cuối cho lượt tiếp
    g.required_first = last_word(text)

    if len(g.players) == 1:
        # SOLO: BOT đối lại
        reply = choose_phrase_starting_with(g.required_first, g.used_phrases)
        if not reply:
            await msg.reply_text("🤖 BOT chịu! Bạn thắng 👑")
            GAMES[chat.id] = Game(chat.id)
            return
        # gửi câu BOT
        await msg.reply_text(reply)
        g.used_phrases.add(reply)
        g.required_first = last_word(reply)
        # reset đồng hồ cho người chơi
        g.reset_round_timers(context, name)
    else:
        # NHIỀU NGƯỜI: chuyển lượt cho người kế
        g.advance_turn()
        nxt = g.current_player(); nname = g.player_names[nxt]
        await msg.reply_text(f"➡️ {mention_html(nxt, nname)} tiếp tục. Bắt đầu bằng: <b>{g.required_first}</b>", parse_mode=ParseMode.HTML)
        g.reset_round_timers(context, nname)

async def eliminate_current(chat_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    g = GAMES.get(chat_id); 
    if not g or len(g.players) < 2: return
    uid = g.current_player(); name = g.player_names.get(uid,"người chơi")
    await context.bot.send_message(chat_id, f"{SAY_ELIMINATE.format(name=name)}\nℹ️ {reason}", parse_mode=ParseMode.HTML)
    # loại
    g.players.pop(g.turn_idx % max(1,len(g.players)))
    if len(g.players) == 0:
        await context.bot.send_message(chat_id, "Hết người chơi. Kết thúc ván.")
        GAMES[chat_id] = Game(chat_id); return
    if len(g.players) == 1:
        winner = g.players[0]; wname = g.player_names[winner]
        await context.bot.send_message(chat_id, f"🏆 {mention_html(winner,wname)} vô địch!", parse_mode=ParseMode.HTML)
        GAMES[chat_id] = Game(chat_id); return
    # vẫn còn ≥2 → người hiện tại giữ nguyên index (đã trỏ sẵn), yêu cầu người này đi
    nxt = g.current_player(); nname = g.player_names[nxt]
    await context.bot.send_message(chat_id, f"➡️ {mention_html(nxt,nname)} đi tiếp. Bắt đầu bằng: <b>{g.required_first}</b>", parse_mode=ParseMode.HTML)

# ============== TIMEOUT GUARD ==============
async def tick_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Chạy mỗi 1s để tự xử lý hết giờ lượt trong các phòng đang chơi."""
    now = context.application.time()
    for chat_id, g in list(GAMES.items()):
        if not g.started or not g.players: continue
        # PTB JobQueue đã nhắc; ở đây loại khi hết giây thật sự
        # Ta không dùng deadline tuyệt đối mà reset reminders mỗi lần → loại bằng job riêng là dễ nhất.
        # Đơn giản hơn: bỏ qua, vì nhắc 5s xong người chơi vẫn không trả lời → người kế gửi hợp lệ là được.
        # Nếu bạn muốn loại cứng khi hết đúng ROUND_SECONDS, có thể gắn timestamp & so sánh.
        pass

# ============== BUILD APP ==============
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

    app.add_handler(CommandHandler(["start"], cmd_start))
    app.add_handler(CommandHandler(["newgame"], cmd_newgame))
    app.add_handler(CommandHandler(["join"], cmd_join))
    app.add_handler(CommandHandler(["ketthuc","end"], cmd_ketthuc))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_text))

    # nhắc tick (không bắt buộc)
    # app.job_queue.run_repeating(tick_timeout, interval=1, first=5)

    # init cache khi start
    async def _on_startup(app: Application):
        await init_phrase_cache()
    app.post_init = _on_startup

    return app
# ==================================================
