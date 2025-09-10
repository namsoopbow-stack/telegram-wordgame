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

# ====== CẤU HÌNH ======
TOKEN = os.environ["TELEGRAM_TOKEN"]
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))  # 60 giây/lượt
MIN_WORD_LEN   = int(os.environ.get("MIN_WORD_LEN", "2"))  # tối thiểu ký tự
DEFAULT_MODE   = os.environ.get("DEFAULT_MODE", "rhyme")
STRICT_DICT    = os.environ.get("STRICT_DICT", "0") == "1" # bắt buộc trong từ điển

# ====== THÔNG BÁO ======
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

# ====== TỪ ĐIỂN (cache offline) ======
DICT_PATH = "dictionary.txt"
HUNSPELL_DIC_URL = "https://raw.githubusercontent.com/1ec5/hunspell-vi/master/dictionaries/vi.dic"

def remove_accents(s: str) -> str:
    nf = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nf if unicodedata.category(ch) != "Mn")

def norm_noaccent_lower(s: str) -> str:
    return remove_accents(s.strip().lower())

def _download_and_build_dictionary(dst_path: str = DICT_PATH) -> int:
    # tải danh sách gốc (root) từ hunspell
    print("[DICT] Downloading Hunspell vi.dic ...")
    with urllib.request.urlopen(HUNSPELL_DIC_URL, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    lines = raw.splitlines()
    # nếu dòng đầu là con số, bỏ
    if lines and lines[0].strip().isdigit():
        lines = lines[1:]

    vocab: Set[str] = set()
    for ln in lines:
        if not ln:
            continue
        token = ln.split("/", 1)[0].strip()
        # giữ chữ, dấu gạch, dấu '
        token = re.sub(r"[^0-9A-Za-zÀ-ỹà-ỹĐđ\s\-']", " ", token, flags=re.UNICODE)
        token = re.sub(r"\s+", " ", token).strip()
        if not token:
            continue
        # lưu cả có dấu & không dấu (để tra linh hoạt)
        token_nd = norm_noaccent_lower(token)
        if len(token_nd) >= 2:
            vocab.add(token_nd)

        token_l = token.lower()
        if len(token_l) >= 2:
            vocab.add(token_l)

    with open(dst_path, "w", encoding="utf-8") as f:
        for w in sorted(vocab):
            f.write(w + "\n")
    print(f"[DICT] Built {dst_path} with {len(vocab)} entries.")
    return len(vocab)

VIET_WORDS: Set[str] = set()
try:
    if not os.path.exists(DICT_PATH) or os.path.getsize(DICT_PATH) < 200_000:
        _download_and_build_dictionary(DICT_PATH)
    with open(DICT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w and not w.startswith("#"):
                VIET_WORDS.add(w)
    print(f"[DICT] Loaded {len(VIET_WORDS)} entries.")
except Exception as e:
    print("[DICT] Cannot load/build dictionary:", e)
    VIET_WORDS = set()

# --- helpers kiểm tra nghĩa & vần ---
VIET_TOKEN_RE = re.compile(r"^[a-zà-ạảãáâầậẩẫấăằặẳẵắèẻẽéêềệểễếìỉĩíòỏõóôồộổỗốơờợởỡớ"
                           r"ùủũúưừựửữứỳỷỹýđ\-']{2,}$", re.IGNORECASE)

def token_valid_loose(w: str) -> bool:
    """nới lỏng: chữ cái VN, dài >=2 (tránh loại nhầm)"""
    w = w.strip()
    return bool(VIET_TOKEN_RE.match(w))

def is_valid_word(w: str) -> bool:
    w_l = w.strip().lower()
    w_nd = norm_noaccent_lower(w)
    in_dict = (w_l in VIET_WORDS) or (w_nd in VIET_WORDS)
    if STRICT_DICT:
        return in_dict
    return in_dict or token_valid_loose(w)

def all_words_valid(phrase: str) -> bool:
    words = [t for t in re.split(r"\s+", phrase.strip()) if t]
    if not words:
        return False
    return all(is_valid_word(t) for t in words)

def rhyme_key(word_or_phrase: str) -> str:
    last = word_or_phrase.strip().split()[-1].lower()
    last_nd = norm_noaccent_lower(last)
    return last_nd[-2:] if len(last_nd) >= 2 else last_nd

def phrase_has_rhyme(phrase: str, target_key: str) -> bool:
    """Đúng nếu TRONG CỤM có ÍT NHẤT 1 từ có vần = target_key."""
    for w in re.split(r"\s+", phrase.strip()):
        if not w:
            continue
        if rhyme_key(w) == target_key:
            return True
    return False

# ====== TRẠNG THÁI VÁN ======
@dataclass
class Match:
    mode: str = DEFAULT_MODE
    active: bool = False
    players: List[int] = field(default_factory=list)
    alive: List[int] = field(default_factory=list)
    turn_idx: int = 0
    current_word: str = ""
    used: Set[str] = field(default_factory=set)
    timer_job_id: Optional[str] = None
    halftime_job_id: Optional[str] = None
    lobby_job_id: Optional[str] = None

MATCHES: Dict[int, Match] = {}

def get_match(chat_id: int) -> Match:
    if chat_id not in MATCHES:
        MATCHES[chat_id] = Match()
    return MATCHES[chat_id]

# ====== HẸN GIỜ ======
async def _cancel_job_by_name(app: Application, name: Optional[str]):
    if not name:
        return
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = get_match(chat_id)
    await _cancel_job_by_name(context.application, match.timer_job_id)
    await _cancel_job_by_name(context.application, match.halftime_job_id)

    half_name = f"half_{chat_id}"
    match.halftime_job_id = half_name
    context.application.job_queue.run_once(
        half_time_notify, when=ROUND_SECONDS // 2, name=half_name, data={"chat_id": chat_id}
    )

    timer_name = f"turn_{chat_id}"
    match.timer_job_id = timer_name
    context.application.job_queue.run_once(
        timeout_eliminate, when=ROUND_SECONDS, name=timer_name, data={"chat_id": chat_id}
    )

async def half_time_notify(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    match = get_match(chat_id)
    if not match.active or not match.alive:
        return
    uid = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(
        chat_id, f"⏳ {member.user.mention_html()} — {random.choice(HALF_TIME_MESSAGES)}",
        parse_mode=ParseMode.HTML
    )

async def timeout_eliminate(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
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
                f"→ Gửi cụm có <b>ít nhất 1 từ</b> vần giống và <b>mọi từ đều có nghĩa</b>.")
    else:
        body = f"👉 {member.user.mention_html()} đi trước. Gửi cụm hợp lệ bất kỳ."
    await context.bot.send_message(chat_id, f"{head}\n{body}", parse_mode=ParseMode.HTML)
    await set_turn_timers(context, chat_id)

# ====== LOBBY AUTO-BEGIN ======
async def schedule_lobby_autobegin(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = get_match(chat_id)
    await _cancel_job_by_name(context.application, match.lobby_job_id)
    name = f"lobby_{chat_id}"
    match.lobby_job_id = name
    # nếu sau 60s không ai /begin, tự bắt đầu (nếu ≥2 người)
    context.application.job_queue.run_once(lobby_autobegin_job, when=60, name=name, data={"chat_id": chat_id})

async def lobby_autobegin_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    match = get_match(chat_id)
    if match.active:
        return
    if len(match.players) >= 2:
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
        "🤖 Bot đối chữ (rhyme) – kiểm tra nghĩa.\n"
        f"⌛ {ROUND_SECONDS}s/lượt (30s có nhắc) • ≥{MIN_WORD_LEN} ký tự\n"
        "Lệnh: /newgame, /join, /begin, /stop, /ping"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    MATCHES[chat_id] = Match()
    await update.message.reply_text("🧩 Tạo sảnh mới. Mọi người /join để tham gia. Sau 1 phút sẽ tự bắt đầu.")
    # auto-join người gọi lệnh
    creator = update.effective_user.id
    MATCHES[chat_id].players.append(creator)
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
    # reset lại đồng hồ lobby (tính lại 60s từ lần tham gia cuối)
    await schedule_lobby_autobegin(context, chat_id)

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy rồi.")
        return
    if len(match.players) < 2:
        await update.message.reply_text("Cần ít nhất 2 người /join mới bắt đầu.")
        return
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
    await update.message.reply_text("⛔ Đã kết thúc ván.")

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

    # loại bỏ tin quá ngắn / không phải chữ
    cleaned = re.sub(r"\s+", " ", text)
    if len(cleaned) < MIN_WORD_LEN or not any(c.isalpha() for c in cleaned):
        eliminated = True
    else:
        ok = True
        # 1) mọi từ đều có nghĩa (nới lỏng nếu STRICT_DICT=0)
        ok = all_words_valid(cleaned)

        # 2) đúng vần (ít nhất một từ trong cụm trùng vần với từ cuối trước đó)
        if ok and match.current_word:
            target = rhyme_key(match.current_word)
            ok = phrase_has_rhyme(cleaned, target)

        # 3) tránh lặp cả cụm (không dấu)
        key = norm_noaccent_lower(cleaned)
        if ok and key in match.used:
            ok = False

        if ok:
            match.used.add(key)
            match.current_word = cleaned
            match.turn_idx = (match.turn_idx + 1) % len(match.alive)
            await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
            await set_turn_timers(context, chat_id)
        else:
            eliminated = True

    if eliminated:
        await update.message.reply_text(f"❌ {random.choice(WRONG_MESSAGES)}")
        match.alive.pop(match.turn_idx)

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
