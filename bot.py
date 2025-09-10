# bot.py
import os
import re
import random
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ====== CẤU HÌNH TỪ ENV ======
TOKEN = os.environ["TELEGRAM_TOKEN"]
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))  # 60 giây/lượt
MIN_WORD_LEN   = int(os.environ.get("MIN_WORD_LEN", "2"))
DEFAULT_MODE   = os.environ.get("DEFAULT_MODE", "rhyme")

# ====== THÔNG BÁO NGẪU NHIÊN ======
HALF_TIME_MESSAGES = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
    "Tắc ẻ đến vậy sao , 30 giây cuối nè :||",
    "30 vẫn chưa phải tết , nhưng mi sắp hết giờ rồi . 30 giây",
    "mắc đitt rặn mẵ không ra . 30 giây cuối ẻ",
    "30 giây cuối ní ơi",
]

WRONG_MESSAGES = [
    "IQ bạn cần phải xem xét lại , mời tiếp !!",
    "Mỗi thế cũng sai , GG cũng không cứu được !",
    "Sai rồi má , Tra lại từ điển đi !",
    "Từ gì vậy má , Học lại lớp 1 đi !!",
    "Ảo tiếng việt hee",
    "Loại , người tiếp theo",
    "Chưa tiến hoá hết à , từ này con người dùng sao . Sai bét!!",
]

TIMEOUT_MESSAGE = "Hết giờ , mời bạn ra ngoài chờ !!"

# ====== TỪ ĐIỂN OFFLINE (TỰ TẠO NẾU CHƯA CÓ) ======
DICT_PATH = "dictionary.txt"
# Nguồn lớn của Hunspell Vietnamese
HUNSPELL_DIC_URL = "https://raw.githubusercontent.com/1ec5/hunspell-vi/master/dictionaries/vi.dic"

def strip_diacritics(s: str) -> str:
    nf = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nf if unicodedata.category(ch) != "Mn")

def normalize_word(w: str) -> str:
    return strip_diacritics(w.strip().lower())

def _download_and_build_dictionary(dst_path: str = DICT_PATH) -> int:
    """
    Tải file .dic của Hunspell, lọc và build dictionary.txt (mỗi dòng 1 mục).
    """
    print("[DICT] Downloading Hunspell vi.dic ...")
    with urllib.request.urlopen(HUNSPELL_DIC_URL, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    lines = raw.splitlines()
    # Dòng đầu có thể là số lượng mục -> bỏ nếu là số
    if lines and lines[0].strip().isdigit():
        lines = lines[1:]

    vocab: Set[str] = set()
    for ln in lines:
        if not ln:
            continue
        # Hunspell có dạng "từ/FLAGS" -> lấy phần trước '/'
        token = ln.split("/", 1)[0].strip()

        # Nới lọc: cho chữ, số, khoảng trắng, gạch nối, dấu nháy
        token = re.sub(r"[^0-9A-Za-zÀ-ỹà-ỹĐđ\s\-']", " ", token, flags=re.UNICODE)
        token = re.sub(r"\s+", " ", token).strip()
        if not token:
            continue
        if len(token) < 2:
            continue

        # Chuẩn hoá không dấu để tra nhanh
        vocab.add(normalize_word(token))

    with open(dst_path, "w", encoding="utf-8") as f:
        for w in sorted(vocab):
            f.write(w + "\n")

    print(f"[DICT] Built {dst_path} with {len(vocab)} words.")
    return len(vocab)

VIET_WORDS: Set[str] = set()
try:
    # Nếu file chưa có hoặc quá nhỏ -> tải & build lại
    if not os.path.exists(DICT_PATH) or os.path.getsize(DICT_PATH) < 500_000:  # ~0.5MB+
        _download_and_build_dictionary(DICT_PATH)
    with open(DICT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if not w or w.startswith("#"):
                continue
            VIET_WORDS.add(w)  # đã normalize sẵn
    print(f"[DICT] Loaded {len(VIET_WORDS)} entries.")
except Exception as e:
    print("[DICT] Cannot load/build dictionary:", e)
    VIET_WORDS = set()

def is_valid_dictionary_word(text: str) -> bool:
    """Dùng từ cuối của cụm, so khớp không dấu trong set VIET_WORDS."""
    if not VIET_WORDS:
        return False
    last = text.strip().split()[-1]
    return normalize_word(last) in VIET_WORDS

# ====== RHYME KEY ======
def rhyme_key(word: str) -> str:
    w = normalize_word(word)
    last = w.split()[-1] if w else ""
    return last[-2:] if len(last) >= 2 else last

# ====== TRẠNG THÁI VÁN ======
@dataclass
class Match:
    mode: str = DEFAULT_MODE
    active: bool = False
    players: List[int] = field(default_factory=list)  # tất cả người đã join
    alive: List[int] = field(default_factory=list)    # người còn trong ván
    turn_idx: int = 0
    current_word: str = ""
    used: Set[str] = field(default_factory=set)
    # timer cho từng lượt
    timer_job_id: Optional[str] = None
    halftime_job_id: Optional[str] = None
    # lobby auto-begin
    lobby_job_id: Optional[str] = None

MATCHES: Dict[int, Match] = {}

def get_match(chat_id: int) -> Match:
    if chat_id not in MATCHES:
        MATCHES[chat_id] = Match()
    return MATCHES[chat_id]

# ====== HẸN GIỜ ======
async def cancel_job_by_name(context: ContextTypes.DEFAULT_TYPE, name: Optional[str]):
    if not name:
        return
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Đặt cả 2 mốc: 30s nhắc & 60s loại"""
    match = get_match(chat_id)
    # huỷ job cũ nếu có
    await cancel_job_by_name(context, match.timer_job_id)
    await cancel_job_by_name(context, match.halftime_job_id)

    # 30s: nhắc
    halftime_name = f"half_{chat_id}"
    match.halftime_job_id = halftime_name
    context.job_queue.run_once(half_time_notify, when=ROUND_SECONDS // 2, name=halftime_name, data={"chat_id": chat_id})

    # 60s: loại
    timer_name = f"turn_{chat_id}"
    match.timer_job_id = timer_name
    context.job_queue.run_once(timeout_eliminate, when=ROUND_SECONDS, name=timer_name, data={"chat_id": chat_id})

async def half_time_notify(ctx):
    chat_id = ctx.job.data["chat_id"]
    context: ContextTypes.DEFAULT_TYPE = ctx.application  # type: ignore
    match = get_match(chat_id)
    if not match.active or not match.alive:
        return
    uid = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, uid)
    msg = random.choice(HALF_TIME_MESSAGES)
    await context.bot.send_message(chat_id, f"⏳ {member.user.mention_html()} — {msg}", parse_mode=ParseMode.HTML)

async def timeout_eliminate(ctx):
    chat_id = ctx.job.data["chat_id"]
    context: ContextTypes.DEFAULT_TYPE = ctx.application  # type: ignore
    match = get_match(chat_id)
    if not match.active or not match.alive:
        return
    uid = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(
        chat_id, f"⏰ {member.user.mention_html()} — {TIMEOUT_MESSAGE}",
        parse_mode=ParseMode.HTML
    )
    match.alive.pop(match.turn_idx)
    if len(match.alive) == 1:
        win_id = match.alive[0]
        win_member = await context.bot.get_chat_member(chat_id, win_id)
        await context.bot.send_message(chat_id, f"🏆 {win_member.user.full_name} thắng! 🎉")
        match.active = False
        match.timer_job_id = None
        match.halftime_job_id = None
        return
    match.turn_idx %= len(match.alive)
    await announce_turn(context, chat_id, match)

async def announce_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match: Match):
    user_id = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, user_id)
    head = f"🔁 Luật: vần • ≥{MIN_WORD_LEN} ký tự • từ phải có nghĩa."
    if match.current_word:
        body = (f"👉 {member.user.mention_html()} đến lượt!\n"
                f"Từ trước: <b>{match.current_word}</b>\n"
                f"→ Gửi từ mới có <b>vần giống</b> và <b>có nghĩa</b>.")
    else:
        body = f"👉 {member.user.mention_html()} đi trước. Gửi từ hợp lệ bất kỳ."
    await context.bot.send_message(chat_id, f"{head}\n{body}", parse_mode=ParseMode.HTML)
    await set_turn_timers(context, chat_id)

# ====== LOBBY AUTO-BEGIN ======
async def schedule_lobby_autobegin(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = get_match(chat_id)
    # huỷ job lobby cũ nếu có
    await cancel_job_by_name(context, match.lobby_job_id)
    name = f"lobby_{chat_id}"
    match.lobby_job_id = name
    context.job_queue.run_once(lobby_autobegin_job, when=60, name=name, data={"chat_id": chat_id})

async def lobby_autobegin_job(ctx):
    chat_id = ctx.job.data["chat_id"]
    context: ContextTypes.DEFAULT_TYPE = ctx.application  # type: ignore
    match = get_match(chat_id)
    if match.active:
        return
    if len(match.players) >= 2:
        # bắt đầu và chọn ngẫu nhiên người đi trước
        match.active = True
        match.alive = list(match.players)
        random.shuffle(match.alive)
        match.turn_idx = 0
        match.current_word = ""
        match.used.clear()
        await context.bot.send_message(chat_id, "⏱️ Hết 1 phút chờ – tự động bắt đầu ván!")
        await announce_turn(context, chat_id, match)
    else:
        await context.bot.send_message(chat_id, "⏱️ Hết 1 phút nhưng chưa đủ người (≥2). Ván chưa thể bắt đầu.")

# ====== COMMANDS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot đối chữ (rhyme) – kiểm tra nghĩa bằng từ điển lớn.\n"
        f"⌛ {ROUND_SECONDS}s/lượt (30s sẽ có nhắc) • ≥{MIN_WORD_LEN} ký tự\n"
        "Lệnh: /newgame, /join, /begin, /stop"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    MATCHES[chat_id] = Match()
    await update.message.reply_text("🧩 Tạo sảnh mới. Mọi người /join để tham gia. Sau 1 phút sẽ tự bắt đầu.")
    # auto-join người gọi lệnh cho chắc
    creator = update.effective_user.id
    MATCHES[chat_id].players.append(creator)
    # đặt hẹn giờ auto-begin
    await schedule_lobby_autobegin(context, chat_id)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    match = get_match(chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy, đợi ván sau.")
        return
    if user_id in match.players:
        await update.message.reply_text("Bạn đã tham gia rồi!")
        return
    match.players.append(user_id)
    await update.message.reply_text(f"✅ {update.effective_user.full_name} đã tham gia ({len(match.players)} người).")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy rồi.")
        return
    if len(match.players) < 2:
        await update.message.reply_text("Cần ít nhất 2 người /join mới bắt đầu.")
        return
    # bắt đầu & random người đi trước
    match.active = True
    match.alive = list(match.players)
    random.shuffle(match.alive)
    match.turn_idx = 0
    match.current_word = ""
    match.used.clear()
    await update.message.reply_text("🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await announce_turn(context, chat_id, match)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(chat_id)
    match.active = False
    match.timer_job_id = None
    match.halftime_job_id = None
    await update.message.reply_text("⏹️ Đã kết thúc ván.")

# ====== XỬ LÝ TIN NHẮN ======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    match = get_match(chat_id)
    if not match.active or not match.alive:
        return
    if user_id != match.alive[match.turn_idx]:
        return

    eliminated = False

    if len(text) < MIN_WORD_LEN:
        eliminated = True
    else:
        ok = True
        # 1) Có nghĩa?
        if not is_valid_dictionary_word(text):
            ok = False
        # 2) Đúng vần?
        if ok and match.current_word:
            ok = rhyme_key(text) != "" and rhyme_key(text) == rhyme_key(match.current_word)
        # 3) Tránh lặp
        key = normalize_word(text)
        if ok and key in match.used:
            ok = False

        if ok:
            match.used.add(key)
            match.current_word = text
            match.turn_idx = (match.turn_idx + 1) % len(match.alive)
            await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
            # đặt lại timer cho người kế tiếp
            await set_turn_timers(context, chat_id)
        else:
            eliminated = True

    if eliminated:
        # mắng vui ngẫu nhiên
        await update.message.reply_text(f"❌ {random.choice(WRONG_MESSAGES)}")
        match.alive.pop(match.turn_idx)

    # kiểm tra thắng/thua
    if match.active:
        if len(match.alive) == 1:
            win_id = match.alive[0]
            mem = await context.bot.get_chat_member(chat_id, win_id)
            await context.bot.send_message(chat_id, f"🏆 {mem.user.full_name} thắng! 🎉")
            match.active = False
            match.timer_job_id = None
            match.halftime_job_id = None
            return
        if eliminated:
            match.turn_idx %= len(match.alive)
            await announce_turn(context, chat_id, match)

# ====== APP ======
def build_application() -> Application:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join",    cmd_join))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
