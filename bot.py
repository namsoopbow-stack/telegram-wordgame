# bot.py
import os, re, random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from unidecode import unidecode

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters,
)

# ================== CẤU HÌNH (ENV) ==================
TOKEN            = os.getenv("TELEGRAM_TOKEN", "").strip()
ROUND_SECONDS    = int(os.getenv("ROUND_SECONDS", "60"))     # 60s mỗi lượt
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", "30"))  # nhắc 30s
AUTO_BEGIN_AFTER = int(os.getenv("AUTO_BEGIN_AFTER", "60"))  # tự bắt đầu sau 60s
MIN_PLAYERS      = int(os.getenv("MIN_PLAYERS", "1"))        # ≥1 người là bắt đầu
MIN_WORD_LEN     = int(os.getenv("MIN_WORD_LEN", "2"))       # mỗi từ ≥2 ký tự
EXACT_WORDS      = 2                                         # bắt buộc đúng 2 từ

# ================== THÔNG ĐIỆP ==================
HALF_WARNINGS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30 vẫn chưa phải tết, nhưng mi sắp hết giờ rồi. 30 giây!",
    "Mắc đ*tt rặn mãi không ra? 30 giây cuối nè!",
    "30 giây cuối ní ơi!",
]
WRONG_REPLIES = [
    "IQ bạn cần phải xem xét lại, mời tiếp!!",
    "Mỗi thế cũng sai, GG cũng không cứu được!",
    "Sai rồi má, tra lại từ điển đi!",
    "Từ gì vậy má, học lại lớp 1 đi!!",
    "Ảo tiếng Việt hee.",
    "Loại, người tiếp theo!",
    "Chưa tiến hoá hết à, từ này con người dùng sao? Sai bét!!",
]
TIMEOUT_REPLY = "Hết giờ, mời bạn ra ngoài chờ!!"

# ================== TỪ ĐIỂN (CHỈ DÙNG PHRASES.TXT) ==================
BASE_DIR = os.path.dirname(__file__)
PHRASES_FP = os.path.join(BASE_DIR, "data", "phrases.txt")   # <-- file bạn tự lưu

def _read_lines(path: str) -> List[str]:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
    except FileNotFoundError:
        # Nếu thiếu file, để rỗng -> mọi câu đều bị sai (đúng yêu cầu: chỉ nhận cụm có trong từ điển)
        pass
    return out

def normalize(s: str) -> str:
    # lower + bỏ dấu + giữ chữ/số/khoảng trắng
    s = s.lower().replace("đ", "d")
    s = unidecode(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Tập cụm 2 từ có nghĩa (chuẩn hoá để so khớp nhanh)
PHRASES: Set[str] = set(normalize(x) for x in _read_lines(PHRASES_FP))

# ================== HÀM VẦN (rhyme) ==================
_VOWEL_KEY_RE = re.compile(r"[aeiouy]+[a-z]*$")   # lấy cụm nguyên âm + phụ âm cuối

def last_word(text: str) -> str:
    toks = normalize(text).split()
    return toks[-1] if toks else ""

def rhyme_key(syllable: str) -> str:
    base = normalize(syllable)
    if not base:
        return ""
    m = _VOWEL_KEY_RE.search(base)
    return m.group(0) if m else (base[-2:] if len(base) >= 2 else base)

def same_rhyme(prev_phrase: Optional[str], new_phrase: str) -> bool:
    if not prev_phrase:
        return True  # lượt đầu tiên, không cần so vần
    return rhyme_key(last_word(prev_phrase)) == rhyme_key(last_word(new_phrase))

# ================== KIỂM TRA LUẬT ==================
def is_two_words(text: str) -> Tuple[bool, List[str]]:
    toks = normalize(text).split()
    if len(toks) != EXACT_WORDS:
        return False, toks
    if any(len(t) < MIN_WORD_LEN for t in toks):
        return False, toks
    return True, toks

def in_dictionary_two_word(text: str) -> bool:
    """Chỉ chấp nhận nếu cụm 2 từ này có trong data/phrases.txt (đã normalize)."""
    ok, toks = is_two_words(text)
    if not ok:
        return False
    norm = " ".join(toks)
    return norm in PHRASES

# ================== TRẠNG THÁI VÁN ==================
@dataclass
class Match:
    chat_id: int
    lobby: Set[int] = field(default_factory=set)
    players: List[int] = field(default_factory=list)
    names: Dict[int, str] = field(default_factory=dict)
    active: bool = False
    turn_idx: int = 0
    current_phrase: Optional[str] = None
    halftime_job: Optional[str] = None
    timeout_job: Optional[str] = None
    autostart_job: Optional[str] = None

ROOMS: Dict[int, Match] = {}

def get_match(cid: int) -> Match:
    if cid not in ROOMS:
        ROOMS[cid] = Match(chat_id=cid)
    return ROOMS[cid]

# ================== HẸN GIỜ ==================
def jobname(kind: str, chat_id: int) -> str:
    return f"{kind}:{chat_id}"

async def cancel_job(context: ContextTypes.DEFAULT_TYPE, name: Optional[str]):
    if not name: return
    for j in context.application.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, m: Match):
    await cancel_job(context, m.halftime_job)
    await cancel_job(context, m.timeout_job)
    hname = jobname("half", m.chat_id)
    tname = jobname("timeout", m.chat_id)
    context.application.job_queue.run_once(half_notify, HALFTIME_SECONDS, name=hname, data=m.chat_id)
    context.application.job_queue.run_once(deadline_kick, ROUND_SECONDS,   name=tname, data=m.chat_id)
    m.halftime_job, m.timeout_job = hname, tname

async def half_notify(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.data
    m = ROOMS.get(cid)
    if not m or not m.active or not m.players: return
    uid = m.players[m.turn_idx]
    await context.bot.send_message(cid, f"⏳ {m.names.get(uid, 'Bạn')}: {random.choice(HALF_WARNINGS)}")

async def deadline_kick(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.data
    m = ROOMS.get(cid)
    if not m or not m.active or not m.players: return
    await context.bot.send_message(cid, f"⏰ {TIMEOUT_REPLY}")
    # loại người tới lượt
    if m.players:
        m.players.pop(m.turn_idx)
    if len(m.players) <= 1:
        await declare_winner(context, m); return
    m.turn_idx %= len(m.players)
    await announce_turn(context, m)

async def declare_winner(context: ContextTypes.DEFAULT_TYPE, m: Match):
    await cancel_job(context, m.halftime_job)
    await cancel_job(context, m.timeout_job)
    m.active = False
    if m.players:
        champ = m.players[0]
        await context.bot.send_message(m.chat_id, f"🏆 {m.names.get(champ, 'người chơi')} là người chiến thắng! Chúc mừng!")
    m.current_phrase = None

# ================== THÔNG BÁO LƯỢT ==================
async def announce_turn(context: ContextTypes.DEFAULT_TYPE, m: Match):
    uid = m.players[m.turn_idx]
    law = f"🔁 Luật: đúng 2 từ • mỗi từ ≥{MIN_WORD_LEN} ký tự • cụm phải có trong từ điển • nối vần theo từ cuối."
    prev = f"Từ trước: {m.current_phrase}" if m.current_phrase else "→ Gửi cụm bất kỳ (nhưng phải có trong từ điển)."
    await context.bot.send_message(m.chat_id, f"{law}\n👉 {m.names.get(uid, 'Bạn')} đến lượt. {prev}")
    await set_turn_timers(context, m)

# ================== LỆNH ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia. "
        "Nếu không ai /begin, bot sẽ tự bắt đầu sau 60s.\n"
        f"Từ điển hiện có: {len(PHRASES)} cụm 2 từ."
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat; user = update.effective_user
    m = get_match(chat.id)
    # reset
    m.lobby.clear(); m.players.clear(); m.active = False
    m.turn_idx = 0; m.current_phrase = None
    await cancel_job(context, m.autostart_job)
    await update.message.reply_text("🎮 Sảnh mở! /join để tham gia. Không ai /begin thì 60s nữa tự bắt đầu.")
    # người tạo auto-join
    m.lobby.add(user.id); m.names[user.id] = user.full_name
    await context.bot.send_message(chat.id, f"➕ {user.full_name} đã tham gia!")
    # đặt auto-begin
    aname = jobname("autobegin", chat.id)
    context.application.job_queue.run_once(auto_begin, AUTO_BEGIN_AFTER, name=aname, data=chat.id)
    m.autostart_job = aname

async def auto_begin(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.data
    m = ROOMS.get(cid)
    if not m or m.active: return
    if len(m.lobby) >= MIN_PLAYERS:
        await begin_game(context, m)
    else:
        await context.bot.send_message(cid, "⏸️ Không đủ người chơi. /newgame để mở lại sảnh.")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = get_match(update.effective_chat.id)
    uid = update.effective_user.id
    if uid not in m.lobby:
        m.lobby.add(uid); m.names[uid] = update.effective_user.full_name
        await update.message.reply_text("Đã tham gia!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = get_match(update.effective_chat.id)
    await begin_game(context, m)

async def begin_game(context: ContextTypes.DEFAULT_TYPE, m: Match):
    if m.active: return
    if len(m.lobby) < MIN_PLAYERS:
        await context.bot.send_message(m.chat_id, "Cần thêm người chơi để bắt đầu."); return
    m.players = list(m.lobby); random.shuffle(m.players)
    m.turn_idx = 0; m.active = True; m.current_phrase = None
    await context.bot.send_message(m.chat_id, "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await announce_turn(context, m)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = get_match(update.effective_chat.id)
    m.active = False
    await cancel_job(context, m.halftime_job)
    await cancel_job(context, m.timeout_job)
    await update.message.reply_text("⛔ Đã dừng game.")

# ================== XỬ LÝ VĂN BẢN ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    cid = update.effective_chat.id; uid = update.effective_user.id
    m = ROOMS.get(cid)
    if not m or not m.active or not m.players: return
    if uid != m.players[m.turn_idx]: return

    text = update.message.text.strip()

    # Luật: 2 từ, có trong từ điển, nối vần
    ok, toks = is_two_words(text)
    if not ok or not in_dictionary_two_word(text) or not same_rhyme(m.current_phrase, text):
        await update.message.reply_text(f"❌ {random.choice(WRONG_REPLIES)}")
        # loại người hiện tại
        m.players.pop(m.turn_idx)
        if len(m.players) <= 1:
            await declare_winner(context, m); return
        m.turn_idx %= len(m.players)
        await announce_turn(context, m)
        return

    # Hợp lệ
    m.current_phrase = text
    await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    m.turn_idx = (m.turn_idx + 1) % len(m.players)
    await announce_turn(context, m)

# ================== APP ==================
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN (Environment > Add Variable)")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join",    cmd_join))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
