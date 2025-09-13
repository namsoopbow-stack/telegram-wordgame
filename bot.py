import os, json, re, html, random, string, asyncio, time
from typing import Dict, List, Optional, Tuple
import aiohttp

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity,
    ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, AIORateLimiter
)

# ===================== CONFIG =====================
BOT_NAME = "SIÊU NHÂN ĐỎ :)"
LOBBY_SECONDS = int(os.getenv("LOBBY_SECONDS", "60"))
TURN_SECONDS  = int(os.getenv("TURN_SECONDS",  "30"))

GIST_ID       = os.environ["GIST_ID"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
DICT_FILE     = "dict_offline.txt"
CLUE_FILE     = "guess_clue_bank.json"

SOHA_URL_1 = "http://tratu.soha.vn/dict/vn_vn/{q}"          # ưu tiên
SOHA_URL_2 = "http://tratu.soha.vn/dict/vn_vn/search/{q}"   # fallback
WIKI_URL   = "https://vi.wiktionary.org/wiki/{q}"           # fallback 2

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Cà khịa
TEASE_WRONG = [
    "Sai bét! Bộ não để trưng à? 🤭",
    "Không đúng! Nhanh tay nhanh não chút coi!",
    "Trật lất rồi cậu bé ơi 😏",
    "Ôi trời ơi, trí tuệ nhân tạo còn ngán cậu!",
    "Nope! Chưa đúng đâu, nghĩ kỹ lại đi.",
    "Đoán vậy thì về đội BOT nhé 😹",
    "Câu trả lời… sai quá sai!",
    "Chưa chuẩn! Cố lên, còn cơ hội.",
    "Hụt rồi nha! Nghe tiếng não chưa?",
    "Trật rồi, đừng nản… nhưng hơi buồn đó!",
    "Sai mà tự tin thật đấy 😆",
    "Thôi thôi, đừng mơ nữa bạn ơi!",
    "Không trúng! Đổi hướng suy nghĩ đi.",
    "Lại sai… Bot bắt đầu xấu hổ dùm bạn 😶",
    "Ối giời, sai một ly đi vài cây số!"
]
TEASE_REMIND = [
    "⏳ Thời gian không chờ ai đâu!",
    "⏳ Còn ít thời gian thôi đấy!",
    "⏳ Nhanh nhanh nào! Sắp hết giờ!",
    "⏳ 30 giây trôi nhanh như crush nhìn người khác đấy!",
    "⏳ Chạy đi chờ chi!",
]
TEASE_5S = ["⏰ Còn 5 giây!", "⏰ 5 giây cuối nè!", "⏰ Nhanh lên!"]

# ===================== TIỆN ÍCH =====================
def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_two_word_phrase(s: str) -> bool:
    return len(norm_space(s).split()) == 2

def last_word(s: str) -> str:
    return norm_space(s).split()[-1]

def first_word(s: str) -> str:
    return norm_space(s).split()[0]

def casefold(s: str) -> str:
    # so sánh mềm (bảo toàn dấu nhưng không phân biệt hoa/thường & khoảng trắng)
    return norm_space(s).casefold()

# ===================== GIST I/O =====================
GIST_API = f"https://api.github.com/gists/{GIST_ID}"

async def gist_get(ctx: ContextTypes.DEFAULT_TYPE, filename: str, default):
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as sess:
        async with sess.get(GIST_API, headers=headers, timeout=30) as r:
            if r.status != 200:
                return default
            data = await r.json()
            files = data.get("files", {})
            if filename not in files or files[filename].get("truncated"):
                # dùng raw_url nếu file lớn
                raw_url = files.get(filename, {}).get("raw_url")
                if not raw_url:
                    return default
                async with sess.get(raw_url, timeout=30) as r2:
                    if r2.status != 200:
                        return default
                    txt = await r2.text()
            else:
                txt = files[filename].get("content") or ""
    try:
        return json.loads(txt) if txt.strip() else default
    except Exception:
        return default

async def gist_put(filename: str, obj):
    payload = {"files": {filename: {"content": json.dumps(obj, ensure_ascii=False, indent=2)}}}
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as sess:
        async with sess.patch(GIST_API, headers=headers, json=payload, timeout=30) as r:
            return r.status in (200, 201)

# ===================== TRA TỪ ONLINE =====================
async def http_get_text(url: str) -> Tuple[int, str]:
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.get(url, timeout=20) as r:
                return r.status, await r.text()
        except Exception:
            return 0, ""

def page_says_not_found(html_text: str) -> bool:
    t = html_text.lower()
    return ("không tìm thấy" in t) or ("khong tim thay" in t) or ("no results" in t)

def page_looks_like_entry(html_text: str) -> bool:
    t = html_text.lower()
    # heuristics cho soha & wiktionary
    return ("từ điển việt" in t) or ("wiktionary" in t and "vi.wiktionary.org" in t) or ("class=\"title\"" in t)

async def check_on_soha(phrase: str) -> Optional[bool]:
    q = aiohttp.helpers.quote(phrase, safe="")
    for url_tpl in (SOHA_URL_1, SOHA_URL_2):
        code, body = await http_get_text(url_tpl.format(q=q))
        if code == 0:
            continue
        if code == 200:
            if page_says_not_found(body):
                return False
            if page_looks_like_entry(body):
                return True
    return None  # không chắc

async def check_on_wiktionary(phrase: str) -> Optional[bool]:
    q = aiohttp.helpers.quote(phrase.replace(" ", "_"), safe="")
    code, body = await http_get_text(WIKI_URL.format(q=q))
    if code == 200 and not page_says_not_found(body):
        return True if page_looks_like_entry(body) else None
    return False

async def phrase_has_meaning(phrase: str, cache: List[str]) -> bool:
    # cache trước
    cf = casefold(phrase)
    if any(casefold(x) == cf for x in cache):
        return True
    # online (Soha → Wiktionary)
    ok = await check_on_soha(phrase)
    if ok is None:
        ok = await check_on_wiktionary(phrase)
    return bool(ok)

# ===================== TRẠNG THÁI =====================
class Lobby:
    def __init__(self, mode: str):
        self.mode = mode               # "doi" | "doan"
        self.players: List[int] = []
        self.started = False
        self.job = None

class DoiChuState:
    def __init__(self, players: List[int], play_with_bot: bool):
        self.players = players[:]      # id
        self.play_with_bot = play_with_bot
        self.turn_idx = 0
        self.current_required: Optional[str] = None   # từ phải bắt đầu
        self.used: List[str] = []
        self.alive = {uid: True for uid in players}
        if play_with_bot:
            self.alive[0] = True  # BOT id 0

class DoanChuState:
    def __init__(self, qid: int, question: str, answer: str, hints: List[str], players: List[int]):
        self.qid = qid
        self.question = question
        self.answer = answer
        self.hints = hints or []
        self.players = players[:]
        self.guess_used = {uid: 0 for uid in players}
        self.alive = {uid: True for uid in players}
        self.start_time = time.time()

chat_lobby: Dict[int, Lobby] = {}
chat_game_doi: Dict[int, DoiChuState] = {}
chat_game_doan: Dict[int, DoanChuState] = {}

# ===================== UI & LUẬT =====================
RULE_DOI = (
    "🎮 *ĐỐI CHỮ*\n"
    "• Cụm *2 từ có nghĩa* (giữ nguyên dấu).\n"
    "• Lượt sau *bắt đầu bằng từ cuối* của cụm trước.\n"
    f"• Mỗi lượt {TURN_SECONDS}s: nhắc 30s & 5s; *sai/ hết giờ = loại*.\n"
    f"• Mở sảnh {LOBBY_SECONDS}s bằng /newgame_doi, mọi người /join để tham gia."
)
RULE_DOAN = (
    "🧩 *ĐOÁN CHỮ*\n"
    "• Bot bốc câu hỏi (tục ngữ/ca dao/thành ngữ...).\n"
    "• Mỗi người có *tối đa 3 lần đoán* trong cả ván; hết lượt = loại.\n"
    f"• Mở sảnh {LOBBY_SECONDS}s bằng /newgame_doan, mọi người /join để tham gia.\n"
    "• Thêm câu mới: `/themcau CÂU HỎI || ĐÁP ÁN || gợi ý1 || gợi ý2` (DM bot hoặc admin group)."
)

# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="pick_doi")],
        [InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="pick_doan")],
    ])
    await update.effective_message.reply_text(
        "Chọn trò nào chơi nè 👇", reply_markup=kb)

async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pick_doi":
        await q.message.reply_text(RULE_DOI, parse_mode=ParseMode.MARKDOWN)
        await q.message.reply_text("Gõ /newgame_doi để mở sảnh 60s, mọi người dùng /join để tham gia.")
    else:
        await q.message.reply_text(RULE_DOAN, parse_mode=ParseMode.MARKDOWN)
        await q.message.reply_text("Gõ /newgame_doan để mở sảnh 60s, mọi người dùng /join để tham gia.")

async def newgame_doi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    chat_lobby[cid] = Lobby("doi")
    await update.effective_message.reply_text(f"🎮 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau {LOBBY_SECONDS}s nếu có người tham gia.")
    job = context.job_queue.run_once(lambda c: asyncio.create_task(begin_game_doi(c, cid)), when=LOBBY_SECONDS)
    chat_lobby[cid].job = job

async def newgame_doan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    chat_lobby[cid] = Lobby("doan")
    await update.effective_message.reply_text(f"🧩 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau {LOBBY_SECONDS}s nếu có người tham gia.")
    job = context.job_queue.run_once(lambda c: asyncio.create_task(begin_game_doan(c, cid)), when=LOBBY_SECONDS)
    chat_lobby[cid].job = job

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    uid = update.effective_user.id
    lobby = chat_lobby.get(cid)
    if not lobby:
        return await update.effective_message.reply_text("Chưa có sảnh. Dùng /newgame_doi hoặc /newgame_doan.")
    if uid not in lobby.players:
        lobby.players.append(uid)
        name = update.effective_user.mention_html()
        await update.effective_message.reply_html(f"✅ {name} đã tham gia!")
    else:
        await update.effective_message.reply_text("Bạn đã trong sảnh rồi.")

# ---------- BẮT ĐẦU GAME ----------
async def begin_game_doi(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    lobby = chat_lobby.get(chat_id)
    if not lobby or lobby.mode != "doi" or lobby.started:
        return
    if not lobby.players:
        await context.bot.send_message(chat_id, "Không ai tham gia. Hủy sảnh.")
        chat_lobby.pop(chat_id, None)
        return
    lobby.started = True
    play_with_bot = len(lobby.players) == 1
    chat_game_doi[chat_id] = DoiChuState(lobby.players, play_with_bot)
    chat_lobby.pop(chat_id, None)

    if play_with_bot:
        await context.bot.send_message(chat_id, f"👤 Chỉ 1 người → *chơi với BOT*.\n✨ Lượt đầu: gửi *cụm 2 từ có nghĩa* bất kỳ.\nSau đó đối tiếp bằng *từ cuối*.", parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id, "👥 Nhiều người tham gia. BOT trọng tài, các bạn đấu với nhau nhé!\n✨ Lượt đầu: gửi *cụm 2 từ có nghĩa* bất kỳ.", parse_mode=ParseMode.MARKDOWN)

    # Reminder job mỗi 30s và ping 5s
    context.job_queue.run_repeating(lambda c: asyncio.create_task(remind_turn_doi(c, chat_id)),
                                    interval=30, first=30, name=f"remind_doi_{chat_id}")

async def begin_game_doan(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    lobby = chat_lobby.get(chat_id)
    if not lobby or lobby.mode != "doan" or lobby.started:
        return
    if not lobby.players:
        await context.bot.send_message(chat_id, "Không ai tham gia. Hủy sảnh.")
        chat_lobby.pop(chat_id, None)
        return
    lobby.started = True
    # Bốc câu hỏi
    bank: List[dict] = await gist_get(context, CLUE_FILE, default=[])
    if not bank:
        await context.bot.send_message(chat_id, "Kho câu hỏi trống. Thêm với /themcau CÂU || ĐÁP || gợi ý …")
        chat_lobby.pop(chat_id, None)
        return
    item = random.choice(bank)
    state = DoanChuState(item.get("id", random.randint(1, 10**9)),
                         item["question"], item["answer"], item.get("hints", []),
                         lobby.players)
    chat_game_doan[chat_id] = state
    chat_lobby.pop(chat_id, None)

    await context.bot.send_message(chat_id,
        f"🧩 *Câu hỏi:* {html.escape(state.question)}\n"
        f"👥 Người chơi: {len(state.players)}\n"
        f"➡️ Mỗi người tối đa *3 lần đoán*. Gõ đáp án ngay!",
        parse_mode=ParseMode.MARKDOWN)

    context.job_queue.run_repeating(lambda c: asyncio.create_task(remind_turn_doan(c, chat_id)),
                                    interval=30, first=30, name=f"remind_doan_{chat_id}")

# ---------- REMINDERS ----------
async def remind_turn_doi(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in chat_game_doi:
        context.job_queue.get_jobs_by_name(f"remind_doi_{chat_id}")
        return
    await context.bot.send_message(chat_id, random.choice(TEASE_REMIND))

async def remind_turn_doan(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in chat_game_doan:
        return
    await context.bot.send_message(chat_id, random.choice(TEASE_REMIND))

# ---------- THÊM CÂU HỎI ----------
def is_admin(update: Update) -> bool:
    return update.effective_chat.type == "private" or update.effective_user.id in getattr(update.effective_chat, "get_administrators", lambda:[])()

async def themcau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = norm_space(update.effective_message.text_html or "")
    # /themcau CÂU || ĐÁP || hint1 || hint2 ...
    parts = [norm_space(p) for p in text.split(" ", 1)[1].split("||")]
    if len(parts) < 2:
        return await update.message.reply_text("Cú pháp: /themcau CÂU HỎI || ĐÁP ÁN || gợi ý1 || gợi ý2 ...")
    q, a = parts[0], parts[1]
    hints = [h for h in (p.strip() for p in parts[2:]) if h]
    bank: List[dict] = await gist_get(context, CLUE_FILE, default=[])
    new_id = (max([it.get("id", 0) for it in bank]) + 1) if bank else 1
    bank.append({"id": new_id, "question": q, "answer": a, "hints": hints})
    ok = await gist_put(CLUE_FILE, bank)
    if ok:
        await update.message.reply_text(f"Đã lưu câu #{new_id} ✅\nTổng: {len(bank)}")
    else:
        await update.message.reply_text("Lưu thất bại (Gist).")

# ===================== XỬ LÝ TIN NHẮN TRONG GAME =====================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    uid = update.effective_user.id
    text = norm_space(update.effective_message.text or "")

    # ĐỐI CHỮ
    if cid in chat_game_doi:
        state = chat_game_doi[cid]
        # chỉ nhận cụm 2 từ
        if not is_two_word_phrase(text):
            return
        # Nếu đang yêu cầu phải bắt đầu bằng từ nào đó
        if state.current_required:
            if first_word(text).casefold() != state.current_required.casefold():
                return await update.message.reply_text(
                    f"❌ Cụm phải *bắt đầu bằng* “{state.current_required}”.", parse_mode=ParseMode.MARKDOWN)

        # Kiểm tra nghĩa (cache → online)
        cache: List[str] = await gist_get(context, DICT_FILE, default=[])
        has = await phrase_has_meaning(text, cache)
        if not has:
            return await update.message.reply_text(f"❌ Cụm không có nghĩa (không tìm thấy). {random.choice(TEASE_WRONG)}")

        # hợp lệ → cập nhật cache nếu chưa có
        if not any(casefold(x) == casefold(text) for x in cache):
            cache.append(text)
            await gist_put(DICT_FILE, cache)

        state.used.append(text)
        state.current_required = last_word(text)

        # Nếu chỉ 1 người → BOT đáp trả (tìm cụm trong cache bắt đầu bằng từ đó)
        if state.play_with_bot:
            await update.message.reply_text(f"✅ Được! Từ chốt: “{state.current_required}”.")
            # BOT tìm trong cache một cụm hợp lệ bắt đầu = từ required
            candidates = [p for p in cache if first_word(p).casefold() == state.current_required.casefold() and p not in state.used]
            if not candidates:
                await context.bot.send_message(cid, "🤖 BOT chịu! Bạn thắng 👑")
                chat_game_doi.pop(cid, None)
                return
            bot_phrase = random.choice(candidates)
            state.used.append(bot_phrase)
            state.current_required = last_word(bot_phrase)
            await context.bot.send_message(cid, f"🤖 BOT: {bot_phrase}\n👉 Lượt bạn. Bắt đầu bằng: “{state.current_required}”.")
        else:
            # Nhiều người: công bố từ chốt; ai cũng có thể gửi lượt tiếp
            await context.bot.send_message(cid, f"✅ Hợp lệ. Từ chốt: “{state.current_required}”.")
        return

    # ĐOÁN CHỮ
    if cid in chat_game_doan:
        st = chat_game_doan[cid]
        if uid not in st.alive or not st.alive[uid]:
            return
        # chặn spam
        st.guess_used[uid] = st.guess_used.get(uid, 0) + 1
        if casefold(text) == casefold(st.answer):
            await update.message.reply_text("🎉 *Chính xác!* Bạn là nhà vô địch!", parse_mode=ParseMode.MARKDOWN)
            chat_game_doan.pop(cid, None)
            return
        else:
            left = 3 - st.guess_used[uid]
            if left <= 0:
                st.alive[uid] = False
                await update.message.reply_text(f"❌ {random.choice(TEASE_WRONG)}\nBạn đã hết 3 lượt → *bị loại*.", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"❌ {random.choice(TEASE_WRONG)}\nBạn còn {left}/3 lượt.")
            # kết thúc nếu mọi người bị loại
            if not any(st.alive.values()):
                await context.bot.send_message(cid, "⏹ Hết người chơi. Kết thúc ván.")
                chat_game_doan.pop(cid, None)
            return

# ===================== LỆNH TIỆN ÍCH =====================
async def stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    chat_lobby.pop(cid, None)
    chat_game_doi.pop(cid, None)
    chat_game_doan.pop(cid, None)
    await update.message.reply_text("⛔ Đã dừng mọi thứ trong phòng.")

# ===================== BOOTSTRAP =====================
def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).rate_limiter(AIORateLimiter()).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_pick, pattern="^pick_"))
    app.add_handler(CommandHandler("newgame_doi", newgame_doi))
    app.add_handler(CommandHandler("newgame_doan", newgame_doan))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("themcau", themcau))
    app.add_handler(CommandHandler("stop", stop_all))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

# ============ cho Render/FastAPI gọi ============
async def initialize(): pass
async def start_polling(): 
    app = build_app()
    await app.initialize(); await app.start(); await app.updater.start_polling()
async def stop(): pass
