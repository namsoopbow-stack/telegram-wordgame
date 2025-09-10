import os
import re
import asyncio
from dataclasses import dataclass, field
from random import choice, shuffle
from typing import Dict, List, Optional

from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================== CẤU HÌNH & THÔNG BÁO ==================
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN trong Environment Variables!")

ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))  # 60s mỗi lượt
HALF_TIME = ROUND_SECONDS // 2

# Chỉ chấp nhận đúng 2 từ
MIN_WORDS = 2
MAX_WORDS = 2

DEFAULT_MODE = os.getenv("DEFAULT_MODE", "rhyme").strip().lower()  # 'rhyme'
STRICT_DICT = int(os.getenv("STRICT_DICT", "0"))  # 0: nới lỏng, 1: siết (giản lược)

HALF_WARNINGS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30 vẫn chưa phải Tết, nhưng mi sắp hết giờ rồi. 30 giây!",
    "Mắc đ*tt rặn mà không ra? 30 giây cuối nè!",
    "30 giây cuối ní ơi!",
]

WRONG_REPLIES = [
    "IQ bạn cần phải xem xét lại, mời tiếp!!",
    "Mỗi thế cũng sai, GG cũng không cứu được!",
    "Sai rồi má, tra lại từ điển đi!",
    "Từ gì vậy má, học lại lớp 1 đi!!",
    "Ảo tiếng Việt heee.",
    "Loại, người tiếp theo.",
    "Chưa tiến hoá hết à? Từ này con người dùng sao? Sai bét!!",
]

TIMEOUT_REPLY = "Hết giờ, mời bạn ra ngoài chờ!!"

RULES_TEXT = "📄 Luật: vần • đúng 2 từ • từ phải có nghĩa."

# ================== TIỆN ÍCH NGÔN NGỮ ==================

# Bỏ dấu + chuẩn hóa lower để so vần đơn giản
VN_MAP = str.maketrans(
    "ÀÁÂẦẤẨẪẬĂẰẮẲẴẶÈÉÊỀẾỂỄỆÌÍÒÓÔỒỐỔỖỘƠỜỚỞỠỢÙÚƯỪỨỬỮỰỲÝĐàáâầấẩẫậăằắẳẵặèéêềếểễệìíòóôồốổỗộơờớởỡợùúưừứửữựỳýđ",
    "AAAAAA AAAAAAEEEEEEEII OOOOOO OOOOO UUUUUUU YYDaaaaaa aaaaaa eeeeeeeii oooooo ooooo uuuuuuu yyd",
)
def norm_noaccent_lower(s: str) -> str:
    return s.translate(VN_MAP).lower()

# Key vần cho tiếng Việt (đơn giản hóa):
# Lấy từ nguyên âm cuối cùng tới hết từ (bao gồm c/ch/m/n/ng/nh/p/t)
VOWELS = "aeiouy"
ENDINGS = ("c","ch","m","n","ng","nh","p","t")
def rhyme_key(word: str) -> str:
    w = norm_noaccent_lower(word)
    last_vowel_idx = -1
    for i in range(len(w)-1, -1, -1):
        if w[i] in VOWELS:
            last_vowel_idx = i
            break
    if last_vowel_idx == -1:
        return w[-2:] if len(w) >= 2 else w
    tail = w[last_vowel_idx:]
    # Ưu tiên các đuôi phổ biến
    for ed in sorted(ENDINGS, key=len, reverse=True):
        if tail.endswith(ed):
            return tail
    return tail

WORD_RE = re.compile(r"[A-Za-zÀ-Ỵà-ỵĐđ]+(?:[-'][A-Za-zÀ-Ỵà-ỵĐđ]+)?", re.UNICODE)
def extract_words(text: str) -> List[str]:
    return WORD_RE.findall(text)

def looks_meaningful(tokens: List[str]) -> bool:
    """Kiểm tra 'có nghĩa' kiểu đơn giản để tránh quá gắt:
       - Mỗi token >= 2 ký tự sau khi bỏ dấu
       - Nếu STRICT_DICT = 1: yêu cầu mạnh hơn (ít nhất 1 nguyên âm mỗi token)
    """
    if len(tokens) != 2:
        return False
    for t in tokens:
        t2 = norm_noaccent_lower(t)
        if len(t2) < 2:
            return False
        if STRICT_DICT:
            if not any(ch in VOWELS for ch in t2):
                return False
    return True

# ================== TRẠNG THÁI TRẬN ==================

@dataclass
class Match:
    chat_id: int
    mode: str = DEFAULT_MODE  # 'rhyme'
    active: bool = False
    alive: List[int] = field(default_factory=list)  # user_ids theo thứ tự lượt
    turn_idx: int = 0
    current_phrase: Optional[str] = None  # "hai tu" normalized
    halftime_job_name: Optional[str] = None
    timeout_job_name: Optional[str] = None
    autostart_job_name: Optional[str] = None

MATCHES: Dict[int, Match] = {}  # chat_id -> Match

# ================== JOB/TIMER ==================

def safe_cancel_job_by_name(context: CallbackContext, name: Optional[str]):
    if not name:
        return
    try:
        for j in context.job_queue.get_jobs_by_name(name):
            j.schedule_removal()
    except Exception:
        pass

async def send_half_warning(context: CallbackContext):
    job = context.job
    chat_id = job.data["chat_id"]
    try:
        await context.bot.send_message(chat_id, f"⏳ {choice(HALF_WARNINGS)}")
    except Exception:
        pass

async def turn_timeout(context: CallbackContext):
    job = context.job
    chat_id = job.data["chat_id"]
    match: Match = job.data["match"]

    # Loại người đang tới lượt
    out_idx = match.turn_idx
    out_uid = match.alive[out_idx]
    mem = await context.bot.get_chat_member(chat_id, out_uid)
    await context.bot.send_message(chat_id, f"❌ {TIMEOUT_REPLY} ({mem.user.first_name})")

    # Cập nhật state
    match.alive.pop(out_idx)
    match.current_phrase = None
    match.halftime_job_name = None
    match.timeout_job_name = None

    # Kết thúc nếu còn 1 người
    if len(match.alive) <= 1:
        if match.alive:
            winner = match.alive[0]
            m = await context.bot.get_chat_member(chat_id, winner)
            await context.bot.send_message(chat_id, f"🏆 {m.user.first_name} chiến thắng! GG!")
        match.active = False
        return

    # Sau khi pop, turn_idx đang trỏ đúng người mới
    await announce_turn(context, match)

def set_turn_timers(context: CallbackContext, chat_id: int, match: Match):
    safe_cancel_job_by_name(context, match.halftime_job_name)
    safe_cancel_job_by_name(context, match.timeout_job_name)

    # Cảnh báo 30s
    half_job = context.job_queue.run_once(
        send_half_warning,
        when=HALF_TIME,
        data={"chat_id": chat_id, "match": match},
        name=f"half-{chat_id}",
    )
    # Hết giờ 60s
    tout_job = context.job_queue.run_once(
        turn_timeout,
        when=ROUND_SECONDS,
        data={"chat_id": chat_id, "match": match},
        name=f"tout-{chat_id}",
    )

    match.halftime_job_name = half_job.name
    match.timeout_job_name = tout_job.name

# ================== THÔNG BÁO LƯỢT ==================

async def announce_turn(context: CallbackContext, match: Match):
    chat_id = match.chat_id
    uid = match.alive[match.turn_idx]
    mem = await context.bot.get_chat_member(chat_id, uid)

    await context.bot.send_message(chat_id, RULES_TEXT)
    await context.bot.send_message(
        chat_id,
        f"👉 {mem.user.first_name} đến lượt! Gửi cụm **2 từ** hợp lệ bất kỳ.",
        parse_mode=ParseMode.MARKDOWN,
    )
    set_turn_timers(context, chat_id, match)

# ================== HANDLERS ==================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello, gõ /newgame để tạo phòng, /join để tham gia, /begin để bắt đầu (hoặc tự bắt đầu sau 60s).")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Hãy thêm bot vào nhóm để chơi nhé!")
        return

    chat_id = chat.id
    m = Match(chat_id=chat_id, mode=DEFAULT_MODE)
    MATCHES[chat_id] = m

    # Người tạo game join luôn
    user_id = update.effective_user.id
    if user_id not in m.alive:
        m.alive.append(user_id)

    await update.message.reply_text("🆕 Tạo phòng mới. Gõ /join để tham gia. Sau **60s** sẽ tự bắt đầu!")
    # Lên lịch tự bắt đầu
    safe_cancel_job_by_name(context, m.autostart_job_name)
    job = context.job_queue.run_once(
        auto_begin_cb,
        when=60,
        data={"chat_id": chat_id},
        name=f"abegin-{chat_id}",
    )
    m.autostart_job_name = job.name

async def auto_begin_cb(context: CallbackContext):
    chat_id = context.job.data["chat_id"]
    m = MATCHES.get(chat_id)
    if not m or m.active:
        return
    if len(m.alive) >= 1:
        shuffle(m.alive)  # ngẫu nhiên người đi trước
        m.turn_idx = 0
        m.active = True
        await context.bot.send_message(chat_id, "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
        await announce_turn(context, m)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = MATCHES.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa có phòng. Gõ /newgame để tạo.")
        return
    uid = update.effective_user.id
    if uid not in m.alive:
        m.alive.append(uid)
        await update.message.reply_text("Đã tham gia!")
    else:
        await update.message.reply_text("Bạn đã trong phòng rồi!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = MATCHES.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa có phòng. /newgame trước nhé.")
        return
    if m.active:
        await update.message.reply_text("Đang chơi rồi!")
        return
    if len(m.alive) < 1:
        await update.message.reply_text("Chưa ai tham gia.")
        return
    shuffle(m.alive)  # ngẫu nhiên người đi trước
    m.turn_idx = 0
    m.active = True
    await update.message.reply_text("🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await announce_turn(context, m)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = MATCHES.get(chat_id)
    if not m:
        await update.message.reply_text("Không có game nào.")
        return
    m.active = False
    safe_cancel_job_by_name(context, m.halftime_job_name)
    safe_cancel_job_by_name(context, m.timeout_job_name)
    safe_cancel_job_by_name(context, m.autostart_job_name)
    await update.message.reply_text("⛔ Đã dừng game.")

# ====== XỬ LÝ VĂN BẢN ======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    m = MATCHES.get(chat_id)
    if not m or not m.active:
        return

    # Không đúng người tới lượt
    if user_id != m.alive[m.turn_idx]:
        return

    tokens = extract_words(update.message.text.strip())
    if len(tokens) != 2:
        await update.message.reply_text(f"❌ {choice(WRONG_REPLIES)}")
        return

    # 'Có nghĩa'
    if not looks_meaningful(tokens):
        await update.message.reply_text(f"❌ {choice(WRONG_REPLIES)}")
        # Loại ngay theo yêu cầu
        out_idx = m.turn_idx
        m.alive.pop(out_idx)
        if len(m.alive) <= 1:
            if m.alive:
                winner = m.alive[0]
                mm = await context.bot.get_chat_member(chat_id, winner)
                await context.bot.send_message(chat_id, f"🏆 {mm.user.first_name} chiến thắng! GG!")
            m.active = False
            return
        await announce_turn(context, m)
        return

    phrase_norm = " ".join(norm_noaccent_lower(t) for t in tokens)

    # Kiểm tra vần (nếu đã có cụm trước)
    if m.mode == "rhyme" and m.current_phrase:
        last_prev = m.current_phrase.split()[-1]
        last_now = norm_noaccent_lower(tokens[-1])
        if rhyme_key(last_prev) != rhyme_key(last_now):
            await update.message.reply_text(f"❌ {choice(WRONG_REPLIES)}")
            # Loại ngay
            out_idx = m.turn_idx
            m.alive.pop(out_idx)
            if len(m.alive) <= 1:
                if m.alive:
                    winner = m.alive[0]
                    mm = await context.bot.get_chat_member(chat_id, winner)
                    await context.bot.send_message(chat_id, f"🏆 {mm.user.first_name} chiến thắng! GG!")
                m.active = False
                return
            await announce_turn(context, m)
            return

    # Hợp lệ → huỷ timer cũ, lưu cụm, chuyển lượt
    safe_cancel_job_by_name(context, m.halftime_job_name)
    safe_cancel_job_by_name(context, m.timeout_job_name)

    m.current_phrase = phrase_norm
    m.turn_idx = (m.turn_idx + 1) % len(m.alive)
    await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    await announce_turn(context, m)

# ================== TẠO APPLICATION ==================
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

application = build_app()
