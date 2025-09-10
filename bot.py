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

# ======== CẤU HÌNH ========
TOKEN            = os.getenv("TELEGRAM_TOKEN", "").strip()
ROUND_SECONDS    = int(os.getenv("ROUND_SECONDS", "60"))       # 60s mỗi lượt
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", "30"))    # nhắc giữa lượt
MIN_WORD_LEN     = int(os.getenv("MIN_WORD_LEN", "2"))         # tối thiểu mỗi từ
EXACT_WORDS      = int(os.getenv("EXACT_WORDS", "2"))          # bắt buộc = 2 từ
AUTO_BEGIN_AFTER = int(os.getenv("AUTO_BEGIN_AFTER", "60"))    # auto begin sau 60s
MIN_PLAYERS      = int(os.getenv("MIN_PLAYERS", "1"))          # >=1 là cho chạy

HALFTIME_HINTS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30 vẫn chưa phải tết, nhưng mi sắp hết giờ rồi. 30 giây!",
    "Mắc đitt rặn mãi không ra? 30 giây cuối nè!",
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

# ======== TỪ ĐIỂN CỤC BỘ ========
BASE_DIR   = os.path.dirname(__file__)
PHRASE_FILE = os.path.join(BASE_DIR, "data", "vi_phrases.txt")
WORD_FILE   = os.path.join(BASE_DIR, "data", "vi_words.txt")

def _read_lines(path: str) -> List[str]:
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    items.append(s)
    except FileNotFoundError:
        pass
    return items

def normalize(s: str) -> str:
    s = s.lower().replace("đ", "d")
    s = unidecode(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

PHRASES: Set[str] = set(normalize(x) for x in _read_lines(PHRASE_FILE))
WORDS:   Set[str] = set(normalize(x) for x in _read_lines(WORD_FILE))

# ======== VẦN ========
_VOWEL_KEY_RE = re.compile(r"[aeiouy]+[a-z]*$")

def last_word(text: str) -> str:
    toks = normalize(text).split()
    return toks[-1] if toks else ""

def rhyme_key(syllable: str) -> str:
    base = normalize(syllable)
    m = _VOWEL_KEY_RE.search(base)
    return (m.group(0) if m else base[-2:]) if base else ""

def same_rhyme(prev_phrase: Optional[str], new_phrase: str) -> bool:
    if not prev_phrase:
        return True
    return rhyme_key(last_word(prev_phrase)) == rhyme_key(last_word(new_phrase))

def is_two_words(text: str) -> Tuple[bool, List[str]]:
    toks = normalize(text).split()
    if len(toks) != EXACT_WORDS:
        return False, toks
    if any(len(t) < MIN_WORD_LEN for t in toks):
        return False, toks
    return True, toks

def is_meaningful_two_word(text: str) -> bool:
    norm = normalize(text)
    if norm in PHRASES:
        return True
    ok, toks = is_two_words(text)
    if not ok:
        return False
    return all(t in WORDS for t in toks)

# ======== TRẠNG THÁI ========
@dataclass
class Match:
    chat_id: int
    thread_id: Optional[int] = None
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

def match_of(chat_id: int) -> Match:
    if chat_id not in ROOMS:
        ROOMS[chat_id] = Match(chat_id=chat_id)
    return ROOMS[chat_id]

# ======== TIỆN ÍCH GỬI TIN ========
async def say(context: ContextTypes.DEFAULT_TYPE, match: Match, text: str):
    await context.bot.send_message(
        match.chat_id, text,
        message_thread_id=match.thread_id
    )

def jobname(kind: str, chat_id: int) -> str:
    return f"{kind}:{chat_id}"

async def cancel_named(context: ContextTypes.DEFAULT_TYPE, name: Optional[str]):
    if not name:
        return
    for j in context.application.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

async def set_timers(context: ContextTypes.DEFAULT_TYPE, match: Match):
    await cancel_named(context, match.halftime_job)
    await cancel_named(context, match.timeout_job)

    hname = jobname("half", match.chat_id)
    tname = jobname("timeout", match.chat_id)

    context.application.job_queue.run_once(half_notify, HALFTIME_SECONDS, name=hname, data=match.chat_id)
    context.application.job_queue.run_once(deadline_kick, ROUND_SECONDS,   name=tname, data=match.chat_id)

    match.halftime_job = hname
    match.timeout_job  = tname

async def half_notify(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    m = ROOMS.get(chat_id)
    if not m or not m.active or not m.players:
        return
    uid = m.players[m.turn_idx]
    name = m.names.get(uid, "Bạn")
    await say(context, m, f"⏳ {name}: {random.choice(HALFTIME_HINTS)}")

async def deadline_kick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    m = ROOMS.get(chat_id)
    if not m or not m.active or not m.players:
        return
    await say(context, m, f"⏰ {TIMEOUT_REPLY}")
    # loại người đang tới lượt
    if m.players:
        m.players.pop(m.turn_idx)
    if len(m.players) <= 1:
        await winner(context, m)
        return
    m.turn_idx %= len(m.players)
    await announce_turn(context, m)

async def winner(context: ContextTypes.DEFAULT_TYPE, m: Match):
    await cancel_named(context, m.halftime_job)
    await cancel_named(context, m.timeout_job)
    m.active = False
    if m.players:
        champ = m.players[0]
        await say(context, m, f"🏆 {m.names.get(champ, 'người chơi')} là người chiến thắng! Chúc mừng!")
    m.current_phrase = None

# ======== LỆNH ========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia. "
        "Bot sẽ tự bắt đầu sau 1 phút nếu đủ người."
    )

async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE, m: Match, uid: int, name: str):
    m.lobby.add(uid)
    m.names[uid] = name
    await say(context, m, f"➕ {name} đã tham gia!")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    m = match_of(chat.id)
    m.thread_id = update.message.message_thread_id  # để gửi vào đúng topic
    # reset
    m.lobby.clear(); m.players.clear()
    m.active = False; m.turn_idx = 0; m.current_phrase = None
    await cancel_named(context, m.autostart_job)

    await update.message.reply_text("🎮 Sảnh đã mở! Gõ /join để tham gia. "
                                    "Nếu không ai /join thêm, bot sẽ tự bắt đầu sau 1 phút.")

    await do_join(update, context, m, user.id, user.full_name)

    name = jobname("autostart", chat.id)
    context.application.job_queue.run_once(auto_begin, AUTO_BEGIN_AFTER, name=name, data=chat.id)
    m.autostart_job = name

async def auto_begin(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    m = ROOMS.get(chat_id)
    if not m or m.active:
        return
    if len(m.lobby) >= MIN_PLAYERS:
        await begin_game(context, m)
    else:
        await say(context, m, "⏸️ Không đủ người chơi. /newgame để mở lại sảnh.")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = match_of(update.effective_chat.id)
    m.thread_id = update.message.message_thread_id or m.thread_id
    await do_join(update, context, m, update.effective_user.id, update.effective_user.full_name)

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = match_of(update.effective_chat.id)
    m.thread_id = update.message.message_thread_id or m.thread_id
    await begin_game(context, m)

async def begin_game(context: ContextTypes.DEFAULT_TYPE, m: Match):
    if m.active:
        return
    if len(m.lobby) < max(1, MIN_PLAYERS):
        await say(context, m, "Cần thêm người chơi để bắt đầu.")
        return
    m.players = list(m.lobby)
    random.shuffle(m.players)
    m.turn_idx = 0
    m.active = True
    m.current_phrase = None
    await say(context, m, "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await announce_turn(context, m)

async def announce_turn(context: ContextTypes.DEFAULT_TYPE, m: Match):
    uid = m.players[m.turn_idx]
    name = m.names.get(uid, "Bạn")
    law  = f"🔁 Luật: vần • {EXACT_WORDS} từ • mỗi từ ≥{MIN_WORD_LEN} ký tự • phải có nghĩa."
    prev = f"Từ trước: {m.current_phrase}" if m.current_phrase else "→ Gửi cụm hợp lệ bất kỳ."
    await say(context, m, f"{law}\n👉 {name} đến lượt. {prev}")
    await set_timers(context, m)

# ======== XỬ LÝ VĂN BẢN ========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    m = ROOMS.get(chat.id)
    if not m or not m.active or not m.players:
        return
    # chỉ người đang tới lượt
    if user.id != m.players[m.turn_idx]:
        return

    text = update.message.text.strip()
    ok2, _ = is_two_words(text)
    if not ok2 or not is_meaningful_two_word(text) or not same_rhyme(m.current_phrase, text):
        await update.message.reply_text(f"❌ {random.choice(WRONG_REPLIES)}")
        # loại người chơi hiện tại
        if m.players:
            m.players.pop(m.turn_idx)
        if len(m.players) <= 1:
            await winner(context, m); return
        m.turn_idx %= len(m.players)
        await announce_turn(context, m)
        return

    # hợp lệ
    m.current_phrase = text
    await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    m.turn_idx = (m.turn_idx + 1) % len(m.players)
    await announce_turn(context, m)

# ======== DEBUG / PING ========
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # log lỗi nhẹ nhàng để không “đỏ” log
    try:
        print("ERROR:", context.error)
    except Exception:
        pass

# ======== APP ========
def get_app() -> Application:
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN – thêm ở Environment của Render.")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join",    cmd_join))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
