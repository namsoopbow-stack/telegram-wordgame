# bot.py
import os, time, re, random, json, asyncio, logging
from collections import deque

import requests
from unidecode import unidecode
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, AIORateLimiter,
    CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("doichu")

# ================== CẤU HÌNH ==================
BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
WAIT_SECONDS    = int(os.getenv("AUTO_START_SECONDS", "60"))     # 60s ở lobby
REMIND_EVERY    = int(os.getenv("REMIND_EVERY_SECONDS", "30"))    # nhắc lobby
TURN_SECONDS    = int(os.getenv("TURN_SECONDS", "30"))            # 30s mỗi lượt

# Từ điển offline (raw URL hoặc đường dẫn file). Ưu tiên URL.
OFFLINE_DICT_URL  = os.getenv("OFFLINE_DICT_URL", "").strip()    # ví dụ: https://gist.githubusercontent.com/.../dict_offline.txt
OFFLINE_DICT_FILE = os.getenv("OFFLINE_DICT_FILE", "dict_vi.txt")

# Gist lưu cache từ đúng
GIST_ID     = os.getenv("GIST_ID", "").strip()                   # ví dụ: 212301c00d2b00247ffc786f921dc29f
GIST_FILE   = os.getenv("GIST_FILE", "dict_offline.txt")         # tên file trong gist
GIST_TOKEN  = os.getenv("GIST_TOKEN", "").strip()

# Wiktionary API (VN)
WIKI_API = "https://vi.wiktionary.org/w/api.php"

# Câu nhắc
NAGS = [
    "⏳ Vẫn chưa có câu à? Mạnh dạn lên!",
    "⌛ Cơ hội không chờ đợi, quất!",
    "🕒 Gần hết giờ đấy, nhanh nào!",
    "📢 Đoán đi chứ! Đừng để cả nhóm đợi!",
    "😴 Chậm thế! Tỉnh táo lên!",
    "🫥 Lỡ nhịp là bị loại đấy!",
    "🧠 IQ chỉ đến thế thôi sao? Nhanh nào!",
    "⚡ Mau! Thời gian bay như gió!",
    "🥵 Đừng run! Bắn câu nào!",
    "🐷 Vẫn chưa ra kết quả? Não heo à!",
]

RIGHT_MSGS = [
    "✅ Ổn áp! Qua lượt!",
    "✅ Chuẩn bài!",
    "✅ Ngon, tiếp tục nào!",
    "✅ Hợp lệ, chuyền bóng!",
]

WRONG_MSGS = [
    "❌ Cụm không có nghĩa (không tìm thấy).",
    "❌ Không hợp lệ rồi!",
    "❌ Sai luật/không thấy nghĩa.",
]

# Lệnh "iu"
ONLY_PING_USER = "@yhck2"

# ================== BỘ NHỚ ==================
# Lobby cho mỗi chat
LOBBY = {}  # chat_id -> {players:set[int], created, count_job, rem_job}

# Trạng thái game cho mỗi chat
GAMES = {}  # chat_id -> GameState

# Bộ nhớ từ điển
DICT_OK = set()   # cụm 2 từ có nghĩa (có dấu), đã biết
DICT_BAD = set()  # từng bị tra không thấy (để đỡ gọi online lại ngay)

# ================== TIỆN ÍCH TỪ ==================
def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_two_word_phrase(s: str) -> bool:
    s = normalize_spaces(s)
    parts = s.split(" ")
    return len(parts) == 2 and all(p for p in parts)

def first_last_word(s: str):
    s = normalize_spaces(s)
    a, b = s.split(" ")
    return a, b

def both_keys(s: str):
    """Tạo hai key để đối chiếu: bản có dấu hạ chuẩn, và bản bỏ dấu."""
    s = normalize_spaces(s)
    return s.lower(), unidecode(s.lower())

# ================== TỪ ĐIỂN OFFLINE/ONLINE ==================
def load_offline_dict():
    """Đổ DICT_OK từ nguồn offline (URL raw hoặc file)."""
    seen = 0
    try:
        if OFFLINE_DICT_URL:
            r = requests.get(OFFLINE_DICT_URL, timeout=10)
            r.raise_for_status()
            lines = r.text.splitlines()
        else:
            if not os.path.exists(OFFLINE_DICT_FILE):
                log.warning("Không thấy OFFLINE_DICT_FILE: %s", OFFLINE_DICT_FILE)
                lines = []
            else:
                with open(OFFLINE_DICT_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
        for ln in lines:
            w = normalize_spaces(ln)
            if is_two_word_phrase(w):
                DICT_OK.add(w.lower())
                DICT_OK.add(unidecode(w.lower()))
                seen += 1
        log.info("Đã nạp %d cụm từ offline.", seen)
    except Exception as e:
        log.exception("Lỗi nạp offline dict: %s", e)

def save_good_to_gist(phrase: str):
    """Lưu cụm đúng vào Gist (append, nếu cấu hình)."""
    if not (GIST_ID and GIST_TOKEN and GIST_FILE):
        return
    try:
        # Lấy gist hiện tại
        gh = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}",
                   "Accept": "application/vnd.github+json"}
        cur = requests.get(gh, headers=headers, timeout=10).json()
        files = cur.get("files", {})
        content = files.get(GIST_FILE, {}).get("content", "")
        # Thêm nếu chưa có
        new_line = phrase.strip()
        if new_line.lower() not in [ln.strip().lower() for ln in content.splitlines()]:
            content = (content + ("\n" if content and not content.endswith("\n") else "")) + new_line + "\n"
            payload = {"files": {GIST_FILE: {"content": content}}}
            requests.patch(gh, headers=headers, data=json.dumps(payload), timeout=10)
    except Exception as e:
        log.warning("Không thể ghi Gist: %s", e)

def online_has_meaning(phrase: str) -> bool:
    """Tra nhanh trên Wiktionary; thấy trang là coi như có nghĩa."""
    try:
        params = {
            "action": "query",
            "format": "json",
            "titles": phrase,
            "redirects": 1,
        }
        r = requests.get(WIKI_API, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        pages = data.get("query", {}).get("pages", {})
        # Trang tồn tại có pageid != -1
        for pid, page in pages.items():
            if pid != "-1":
                return True
        return False
    except Exception as e:
        log.warning("Online check lỗi: %s", e)
        return False

def is_valid_phrase(phrase: str) -> bool:
    """Kiểm tra hợp lệ: 2 từ & có nghĩa (offline trước, không có → online). Cache kết quả."""
    phrase = normalize_spaces(phrase)
    if not is_two_word_phrase(phrase):
        return False
    key_lc, key_no = both_keys(phrase)

    # Tránh spam online
    if key_lc in DICT_BAD or key_no in DICT_BAD:
        return False

    # Offline
    if key_lc in DICT_OK or key_no in DICT_OK:
        return True

    # Online
    if online_has_meaning(phrase):
        DICT_OK.add(key_lc); DICT_OK.add(key_no)
        # Lưu vĩnh viễn
        save_good_to_gist(phrase)
        return True

    # cache xấu
    DICT_BAD.add(key_lc); DICT_BAD.add(key_no)
    return False

# ================== LOBBY ==================
async def _auto_begin_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    app = ctx.application
    state = LOBBY.get(chat_id)
    if not state:
        return
    # hủy job nhắc
    try:
        if state.get("rem_job"): state["rem_job"].schedule_removal()
    except: ...
    players = list(state["players"])
    LOBBY.pop(chat_id, None)

    if len(players) == 0:
        await app.bot.send_message(chat_id, "⌛ Hết giờ mà chưa có ai join. Đóng sảnh!")
        return
    await _start_game(app, chat_id, players)

async def _remind_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    app = ctx.application
    state = LOBBY.get(chat_id)
    if not state: return
    since = int(time.time() - state["created"])
    remain = max(0, WAIT_SECONDS - since)
    if remain <= 0: return
    msg = f"{random.choice(NAGS)}\n🕰️ Còn {remain}s!"
    await app.bot.send_message(chat_id, msg)

async def newgame_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    app = context.application
    # reset lobby
    old = LOBBY.get(chat_id)
    if old:
        try:
            if old.get("count_job"): old["count_job"].schedule_removal()
            if old.get("rem_job"): old["rem_job"].schedule_removal()
        except: ...
        LOBBY.pop(chat_id, None)

    LOBBY[chat_id] = {"players": set(), "created": time.time(), "count_job": None, "rem_job": None}
    await update.effective_message.reply_text(
        "🎮 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau 60s nếu có người tham gia."
    )
    count_job = app.job_queue.run_once(_auto_begin_job, when=WAIT_SECONDS, chat_id=chat_id)
    rem_job = app.job_queue.run_repeating(_remind_job, interval=REMIND_EVERY, first=REMIND_EVERY, chat_id=chat_id)
    LOBBY[chat_id]["count_job"] = count_job
    LOBBY[chat_id]["rem_job"]   = rem_job

async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    state = LOBBY.get(chat_id)
    if not state:
        await update.effective_message.reply_text("❌ Chưa có sảnh. Dùng /newgame để mở.")
        return
    state["players"].add(uid)
    await update.effective_message.reply_text(
        f"✅ <b>{update.effective_user.full_name}</b> đã tham gia!", parse_mode=ParseMode.HTML
    )

async def begin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    app = context.application
    state = LOBBY.get(chat_id)
    if not state:
        await update.effective_message.reply_text("❌ Chưa có sảnh. Dùng /newgame để mở.")
        return
    try:
        if state.get("count_job"): state["count_job"].schedule_removal()
        if state.get("rem_job"): state["rem_job"].schedule_removal()
    except: ...
    players = list(state["players"])
    LOBBY.pop(chat_id, None)
    if len(players) == 0:
        await update.effective_message.reply_text("⌛ Chưa có ai /join. Hủy bắt đầu.")
        return
    await _start_game(app, chat_id, players)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = LOBBY.pop(chat_id, None)
    if st:
        try:
            if st.get("count_job"): st["count_job"].schedule_removal()
            if st.get("rem_job"): st["rem_job"].schedule_removal()
        except: ...
        await update.effective_message.reply_text("🛑 Đóng sảnh, huỷ đếm ngược.")
    else:
        await update.effective_message.reply_text("ℹ️ Không có sảnh nào đang mở.")

# ================== GAMEPLAY ==================
class GameState:
    def __init__(self, chat_id: int, players: list[int]):
        self.chat_id = chat_id
        self.players = deque(players)  # xoay vòng
        self.mode = "solo" if len(players) == 1 else "multi"
        self.current = self.players[0]
        self.last_phrase = None
        self.tail = None
        self.used = set()              # tránh lặp
        self.turn_job = None

    def rotate_next(self):
        self.players.rotate(-1)
        self.current = self.players[0]

async def _start_turn(app: Application, gs: GameState):
    """Bắt đầu / reset bộ đếm cho 1 lượt."""
    # hủy job cũ
    try:
        if gs.turn_job: gs.turn_job.schedule_removal()
    except: ...
    # đặt job nhắc + timeout
    async def tick(ctx: ContextTypes.DEFAULT_TYPE):
        # nhắc ở 25s -> “còn 5s”
        await app.bot.send_message(gs.chat_id, "⏰ Còn 5 giây!")

    async def timeout(ctx: ContextTypes.DEFAULT_TYPE):
        # Hết giờ -> loại nếu multi, kết thúc nếu solo
        if gs.mode == "multi":
            kicked = gs.current
            await app.bot.send_message(
                gs.chat_id,
                f"⏱️ Hết giờ lượt! <a href='tg://user?id={kicked}'>người này</a> bị loại.",
                parse_mode=ParseMode.HTML
            )
            # loại
            try:
                gs.players.remove(kicked)
            except: ...
            if len(gs.players) <= 1:
                await app.bot.send_message(gs.chat_id, "🏆 Hết người chơi. Kết thúc ván.")
                GAMES.pop(gs.chat_id, None)
                return
            gs.current = gs.players[0]
            await _announce_turn(app, gs)
        else:
            await app.bot.send_message(gs.chat_id, "⏱️ Hết giờ! BOT thắng 👑")
            GAMES.pop(gs.chat_id, None)

    # lên lịch: nhắc 25s, timeout 30s
    app.job_queue.run_once(tick, when=TURN_SECONDS - 5, chat_id=gs.chat_id)
    gs.turn_job = app.job_queue.run_once(timeout, when=TURN_SECONDS, chat_id=gs.chat_id)

async def _announce_turn(app: Application, gs: GameState):
    if gs.mode == "solo":
        await app.bot.send_message(
            gs.chat_id,
            "🧍 Chỉ 1 người → chơi với BOT.\n✨ Gửi **cụm 2 từ có nghĩa** bất kỳ."
            + (f"\n➡️ Phải bắt đầu bằng **{gs.tail}**." if gs.tail else ""),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await app.bot.send_message(
            gs.chat_id,
            f"🎯 Lượt của <a href='tg://user?id={gs.current}'>người này</a>"
            + (f" — bắt đầu bằng <b>{gs.tail}</b>." if gs.tail else " — mở màn, gửi cụm bất kỳ."),
            parse_mode=ParseMode.HTML
        )
    await _start_turn(app, gs)

async def _start_game(app: Application, chat_id: int, players: list[int]):
    random.shuffle(players)
    gs = GameState(chat_id, players)
    GAMES[chat_id] = gs

    if gs.mode == "solo":
        await app.bot.send_message(chat_id,
            "🧍 Chỉ 1 người → chơi với BOT.\n✨ Lượt đầu: gửi **cụm 2 từ có nghĩa** bất kỳ.\nSau đó đối tiếp bằng **từ cuối**.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await app.bot.send_message(chat_id,
            f"👥 {len(players)} người tham gia. BOT làm trọng tài.\n✨ Người mở màn: <a href='tg://user?id={gs.current}'>người này</a>.",
            parse_mode=ParseMode.HTML
        )
    await _announce_turn(app, gs)

def _fails_reason(phrase: str, gs: GameState):
    phrase = normalize_spaces(phrase)
    if not is_two_word_phrase(phrase):
        return "Câu phải gồm **2 từ** (cụm 2 từ)."
    if gs.tail:
        a, b = first_last_word(phrase)
        if a.lower() != gs.tail.lower():
            return f"Câu phải bắt đầu bằng **{gs.tail}**."
    if phrase.lower() in gs.used:
        return "Cụm đã dùng rồi."
    if not is_valid_phrase(phrase):
        return "Cụm không có nghĩa (không tìm thấy)."
    return None

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    gs = GAMES.get(chat_id)
    if not gs:
        return  # không ở trong ván

    # Chỉ người đến lượt mới được đánh (trong multi)
    if gs.mode == "multi" and uid != gs.current:
        return

    reason = _fails_reason(text, gs)
    if reason:
        if gs.mode == "multi":
            await update.effective_message.reply_text(
                f"❌ {reason}\n➡️ <a href='tg://user?id={uid}'>bạn</a> bị loại.",
                parse_mode=ParseMode.HTML
            )
            try:
                gs.players.remove(uid)
            except: ...
            if len(gs.players) <= 1:
                await context.bot.send_message(chat_id, "🏆 Hết người chơi. Kết thúc ván.")
                GAMES.pop(chat_id, None)
                return
            gs.current = gs.players[0]
            await _announce_turn(context.application, gs)
        else:
            await update.effective_message.reply_text(f"❌ {reason}\n👑 BOT thắng!")
            GAMES.pop(chat_id, None)
        return

    # Hợp lệ
    gs.used.add(text.lower())
    _, tail = first_last_word(text)
    gs.last_phrase = text
    gs.tail = tail
    await update.effective_message.reply_text(random.choice(RIGHT_MSGS))

    if gs.mode == "multi":
        gs.rotate_next()
        await _announce_turn(context.application, gs)
    else:
        # Solo: tiếp tục kiểm tra lượt sau (không cần BOT đối từ)
        await _announce_turn(context.application, gs)

# ================== LỆNH KHÁC ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Chào nhóm!\n"
        "• /newgame → mở sảnh, mọi người /join để tham gia (tự bắt đầu sau 60s).\n"
        "• /begin → bắt đầu ngay.\n"
        "• /stop → đóng sảnh (nếu đang mở).\n"
        "Luật: đối **cụm 2 từ có nghĩa**. Lượt sau phải bắt đầu bằng **từ thứ 2** của cụm trước.\n"
        "Mỗi lượt 30s, sai hoặc hết giờ sẽ bị loại.",
        parse_mode=ParseMode.MARKDOWN
    )

async def iu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # chỉ cho phép người dùng cụ thể
    if update.effective_user and (update.effective_user.username or ""):
        atname = "@" + update.effective_user.username
        if atname.lower() == ONLY_PING_USER.lower():
            await update.effective_message.reply_text("Anh Nam Yêu Em Thiệu ❤️")
            return
    await update.effective_message.reply_text("iu gì mà iu 😏")

# ================== APP ==================
async def initialize(app: Application):
    load_offline_dict()
    log.info("Init xong.")

async def stop(app: Application):
    pass

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

    app.add_handler(CommandHandler(["start"], start_cmd))
    app.add_handler(CommandHandler(["newgame","batdau"], newgame_cmd))
    app.add_handler(CommandHandler(["join","thamgia"], join_cmd))
    app.add_handler(CommandHandler(["begin","batdau_ngay"], begin_cmd))
    app.add_handler(CommandHandler(["stop","ketthuc"], stop_cmd))
    app.add_handler(CommandHandler(["iu"], iu_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app
