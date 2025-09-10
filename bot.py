# bot.py
import os
import random
import asyncio
from typing import Dict, List, Optional, Set, Tuple

from unidecode import unidecode
from telegram import Update, Message, Chat, User
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ======= ENV =======
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))        # thời gian 1 lượt
HALFTIME_SECONDS = int(os.environ.get("HALFTIME_SECONDS", "30"))  # mốc nhắc 30s
AUTO_BEGIN_SECONDS = int(os.environ.get("AUTO_BEGIN_SECONDS", "60"))  # /newgame xong không ai /begin thì tự bắt đầu
DICT_FILE = os.environ.get("DICT_FILE", "dict_vi.txt")
MIN_WORD_LEN = int(os.environ.get("MIN_WORD_LEN", "2"))
MIN_PHRASE_WORDS = int(os.environ.get("MIN_PHRASE_WORDS", "2"))
MAX_PHRASE_WORDS = int(os.environ.get("MAX_PHRASE_WORDS", "2"))

# ======= PHRASES =======
HALF_WARNINGS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời :))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30s vẫn chưa phải Tết, nhưng mi sắp hết giờ rồi!",
    "Mắc đitt rặn mãi không ra à? 30 giây cuối nè!",
    "30 giây cuối ní ơi!"
]
WRONG_REPLIES = [
    "IQ bạn cần phải xem xét lại, mời tiếp!!",
    "Mỗi thế cũng sai, GG cũng không cứu được!",
    "Sai rồi má, tra lại từ điển đi!",
    "Từ gì vậy má, học lại lớp 1 đi!!",
    "Ảo tiếng Việt hê!",
    "Loại, người tiếp theo!",
    "Chưa tiến hoá hết à, từ này con người dùng sao. Sai bét!!"
]
TIMEOUT_REPLY = "⏰ Hết giờ, mời bạn ra ngoài chờ!!"
HARD_ELIMINATE = "❌ 傻逼 Cấm Cãi !!!"

# ======= DICTIONARY =======
def load_dict(path: str) -> Set[str]:
    s: Set[str] = set()
    if not os.path.exists(path):
        return s
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = " ".join(line.strip().lower().split())
            if w:
                s.add(w)
    return s

DICT: Set[str] = load_dict(DICT_FILE)

# ======= GAME STATE =======
class Match:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.joined: List[int] = []
        self.turn_idx: int = 0
        self.active: bool = False
        self.current_phrase: Optional[str] = None
        self.used: Set[str] = set()

        # Jobs
        self.job_half_name: Optional[str] = None
        self.job_full_name: Optional[str] = None
        self.job_autobegin_name: Optional[str] = None

    def reset(self):
        self.turn_idx = 0
        self.active = False
        self.current_phrase = None
        self.used.clear()
        self.cancel_timers()

    def cancel_timers(self):
        self.job_half_name = None
        self.job_full_name = None

# chat_id -> Match
ROOMS: Dict[int, Match] = {}

# ======= UTILS =======
def normalize_phrase(text: str) -> str:
    return " ".join(text.strip().lower().split())

def count_words(text: str) -> int:
    return len(normalize_phrase(text).split())

def is_valid_phrase(text: str) -> bool:
    """Hợp lệ khi:
    - đúng 2 từ (2 vần)
    - mỗi từ có >= MIN_WORD_LEN ký tự
    - cụm có nghĩa (có trong DICT)
    """
    t = normalize_phrase(text)
    parts = t.split()
    if len(parts) != 2:
        return False
    if any(len(p) < MIN_WORD_LEN for p in parts):
        return False
    return t in DICT

async def say_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "📘 Luật: đúng **2 từ** • mỗi từ ≥2 ký tự • mỗi từ **phải có nghĩa** (nằm trong từ điển).",
        parse_mode=ParseMode.MARKDOWN
    )

def current_player_id(m: Match) -> Optional[int]:
    if not m.joined:
        return None
    return m.joined[m.turn_idx % len(m.joined)]

async def announce_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, m: Match):
    chat_id = m.chat_id
    uid = current_player_id(m)
    if uid is None:
        return
    user = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(
        chat_id,
        f"🟢 {user.user.first_name} đi trước. Gửi **cụm 2 từ** có nghĩa bất kỳ.",
        parse_mode=ParseMode.MARKDOWN
    )
    await set_turn_timers(context, chat_id)

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Đặt nhắc 30s và loại sau 60s cho người đang tới lượt."""
    app = context.application
    if app is None or app.job_queue is None:
        return  # job-queue chưa sẵn sàng

    m = ROOMS.get(chat_id)
    if not m or not m.active:
        return

    # Hủy timer cũ
    if m.job_half_name:
        app.job_queue.scheduler.remove_job(m.job_half_name, jobstore='default', quiet=True)
    if m.job_full_name:
        app.job_queue.scheduler.remove_job(m.job_full_name, jobstore='default', quiet=True)

    # Tạo tên job riêng cho từng chat
    m.job_half_name = f"half_{chat_id}"
    m.job_full_name = f"full_{chat_id}"

    # callback
    async def half_warn_cb(ctx: ContextTypes.DEFAULT_TYPE):
        # Nếu người tới lượt chưa nhắn gì mới
        if m.active:
            warn = random.choice(HALF_WARNINGS)
            await ctx.bot.send_message(chat_id, f"⚠️ {warn}")

    async def full_timeout_cb(ctx: ContextTypes.DEFAULT_TYPE):
        if not m.active:
            return
        uid = current_player_id(m)
        if uid is None:
            return
        user = await ctx.bot.get_chat_member(chat_id, uid)
        await ctx.bot.send_message(chat_id, f"{TIMEOUT_REPLY}\n👉 {user.user.first_name} bị loại.")
        # loại người chơi
        if uid in m.joined:
            m.joined.remove(uid)
        if len(m.joined) < 2:
            m.active = False
            await ctx.bot.send_message(chat_id, "🏁 Ván kết thúc (không đủ người).")
            return
        # chuyển lượt
        m.turn_idx %= len(m.joined)
        await announce_turn(Update(update_id=0), ctx, m)

    # Đặt job
    app.job_queue.run_once(half_warn_cb, when=HALFTIME_SECONDS, chat_id=chat_id, name=m.job_half_name)
    app.job_queue.run_once(full_timeout_cb, when=ROUND_SECONDS, chat_id=chat_id, name=m.job_full_name)

async def cancel_autobegin(context: ContextTypes.DEFAULT_TYPE, m: Match):
    if context.application and context.application.job_queue and m.job_autobegin_name:
        try:
            context.application.job_queue.scheduler.remove_job(m.job_autobegin_name, jobstore='default', quiet=True)
        except Exception:
            pass
    m.job_autobegin_name = None

async def schedule_autobegin(update: Update, context: ContextTypes.DEFAULT_TYPE, m: Match):
    """/newgame xong nếu không ai /begin trong AUTO_BEGIN_SECONDS thì tự bắt đầu."""
    if not context.application or not context.application.job_queue:
        return
    chat_id = m.chat_id
    m.job_autobegin_name = f"auto_begin_{chat_id}"

    async def do_begin(ctx: ContextTypes.DEFAULT_TYPE):
        if m.active:
            return
        if len(m.joined) >= 2:
            await ctx.bot.send_message(chat_id, "🚀 Không ai /begin – tự động bắt đầu!")
            # chọn ngẫu nhiên người đi trước
            random.shuffle(m.joined)
            m.turn_idx = 0
            m.active = True
            await say_rules(update, ctx)
            await announce_turn(update, ctx, m)

    context.application.job_queue.run_once(do_begin, when=AUTO_BEGIN_SECONDS, chat_id=chat_id, name=m.job_autobegin_name)

# ======= COMMANDS =======
async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Chỉ chơi trong nhóm!")
        return

    m = ROOMS.get(chat.id) or Match(chat.id)
    ROOMS[chat.id] = m
    m.reset()
    # auto thêm người gọi /newgame
    user = update.effective_user
    if user and user.id not in m.joined:
        m.joined.append(user.id)

    await update.message.reply_text("🎮 Sảnh mở! /join để tham gia. Không ai /begin thì "
                                    f"{AUTO_BEGIN_SECONDS}s nữa tự bắt đầu.")
    await schedule_autobegin(update, context, m)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = ROOMS.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa mở sảnh. Dùng /newgame trước.")
        return
    uid = update.effective_user.id
    if uid not in m.joined:
        m.joined.append(uid)
        await update.message.reply_text("✅ Đã tham gia!")
    else:
        await update.message.reply_text("Bạn đã ở trong sảnh.")
    # nếu đã có ≥2 người thì có thể bắt đầu bất kỳ lúc nào

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = ROOMS.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa mở sảnh. /newgame trước.")
        return
    if m.active:
        await update.message.reply_text("Đang chơi rồi.")
        return
    if len(m.joined) < 2:
        await update.message.reply_text("Cần tối thiểu 2 người. /join thêm bạn!")
        return

    await cancel_autobegin(context, m)
    random.shuffle(m.joined)
    m.turn_idx = 0
    m.active = True

    await update.message.reply_text("🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await say_rules(update, context)
    await announce_turn(update, context, m)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = ROOMS.get(chat_id)
    if not m:
        await update.message.reply_text("Không có ván nào.")
        return
    m.reset()
    await update.message.reply_text("⛔ Đã dừng ván hiện tại.")

# ======= TEXT HANDLER =======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message
    if not msg or not msg.text:
        return
    chat_id = msg.chat_id
    m = ROOMS.get(chat_id)
    if not m or not m.active:
        return

    # chỉ người tới lượt được trả lời
    uid = update.effective_user.id
    if uid != current_player_id(m):
        return

    phrase = normalize_phrase(msg.text)

    # Luật “đúng 2 từ” – nếu 1 hoặc ≥3 từ: loại ngay, trả câu HARD_ELIMINATE
    wc = count_words(phrase)
    if wc != 2:
        await msg.reply_text(HARD_ELIMINATE)
        # loại
        if uid in m.joined:
            m.joined.remove(uid)
        if len(m.joined) < 2:
            m.active = False
            await context.bot.send_message(chat_id, "🏁 Ván kết thúc (không đủ người).")
            return
        m.turn_idx %= len(m.joined)
        await announce_turn(update, context, m)
        return

    # kiểm tra có nghĩa + độ dài mỗi từ
    if not is_valid_phrase(phrase):
        await msg.reply_text(f"❌ {random.choice(WRONG_REPLIES)}")
        # loại
        if uid in m.joined:
            m.joined.remove(uid)
        if len(m.joined) < 2:
            m.active = False
            await context.bot.send_message(chat_id, "🏁 Ván kết thúc (không đủ người).")
            return
        m.turn_idx %= len(m.joined)
        await announce_turn(update, context, m)
        return

    # Hợp lệ -> chuyển lượt
    m.used.add(phrase)
    m.current_phrase = phrase
    m.turn_idx = (m.turn_idx + 1) % len(m.joined)
    await msg.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    await announce_turn(update, context, m)

# ======= APP FACTORY =======
def build_app() -> Application:
    # JobQueue sẽ tự bật khi đã cài gói [job-queue] trong requirements.txt
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
