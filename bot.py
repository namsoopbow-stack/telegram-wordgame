# bot.py
import os
import re
import unicodedata
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
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))
MIN_WORD_LEN   = int(os.environ.get("MIN_WORD_LEN", "2"))
DEFAULT_MODE   = os.environ.get("DEFAULT_MODE", "rhyme")  # hiện dùng 'rhyme'

# ====== TỪ ĐIỂN OFFLINE ======
DICT_PATH = "dictionary.txt"

def strip_diacritics(s: str) -> str:
    nf = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nf if unicodedata.category(ch) != "Mn")

def normalize_word(w: str) -> str:
    """Chuẩn hoá để so khớp từ điển: bỏ dấu, lower, gọn khoảng trắng."""
    return strip_diacritics(w.strip().lower())

# Tải từ điển
VIET_WORDS: Set[str] = set()
if os.path.exists(DICT_PATH):
    with open(DICT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if not w or w.startswith("#"):  # cho phép comment
                continue
            VIET_WORDS.add(normalize_word(w))
else:
    print(f"[WARN] Không tìm thấy {DICT_PATH}. Mọi từ sẽ bị coi là KHÔNG có nghĩa.")

def is_valid_dictionary_word(text: str) -> bool:
    """
    - Lấy từ cuối cùng trong cụm (ví dụ 'cá voi' -> 'voi')
    - So khớp không dấu trong từ điển
    """
    if not VIET_WORDS:
        return False  # không có từ điển thì coi như không hợp lệ (để bạn bổ sung sớm)
    last = text.strip().split()[-1]
    return normalize_word(last) in VIET_WORDS

# ====== KHÓA VẦN (rhyme) ======
def rhyme_key(word: str) -> str:
    w = normalize_word(word)
    last = w.split()[-1] if w else ""
    return last[-2:] if len(last) >= 2 else last

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

MATCHES: Dict[int, Match] = {}

def get_match(chat_id: int) -> Match:
    if chat_id not in MATCHES:
        MATCHES[chat_id] = Match()
    return MATCHES[chat_id]

# ====== HẸN GIỜ/LƯỢT ======
async def set_turn_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = get_match(chat_id)
    if match.timer_job_id:
        for j in context.job_queue.get_jobs_by_name(match.timer_job_id):
            j.schedule_removal()
    job_name = f"turn_{chat_id}"
    match.timer_job_id = job_name
    context.job_queue.run_once(timeout_eliminate, when=ROUND_SECONDS, name=job_name, data={"chat_id": chat_id})

async def timeout_eliminate(ctx):
    chat_id = ctx.job.data["chat_id"]
    context: ContextTypes.DEFAULT_TYPE = ctx.application  # type: ignore
    match = get_match(chat_id)
    if not match.active or not match.alive:
        return
    uid = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(
        chat_id, f"⏰ Hết {ROUND_SECONDS}s – {member.user.mention_html()} bị loại!",
        parse_mode=ParseMode.HTML
    )
    match.alive.pop(match.turn_idx)
    if len(match.alive) == 1:
        win_id = match.alive[0]
        win_member = await context.bot.get_chat_member(chat_id, win_id)
        await context.bot.send_message(chat_id, f"🏆 {win_member.user.full_name} thắng! 🎉")
        match.active = False
        match.timer_job_id = None
        return
    match.turn_idx %= len(match.alive)
    await announce_turn(context, chat_id, match)

async def announce_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match: Match):
    user_id = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, user_id)
    head = f"🔁 Luật: vần • tối thiểu {MIN_WORD_LEN} ký tự • kiểm tra nghĩa theo từ điển."
    if match.current_word:
        body = (f"👉 {member.user.mention_html()} đến lượt!\n"
                f"Từ trước: <b>{match.current_word}</b>\n"
                f"→ Gửi từ mới có <b>vần giống</b> và <b>có nghĩa</b>.")
    else:
        body = f"👉 {member.user.mention_html()} đi trước. Gửi bất kỳ từ hợp lệ."
    await context.bot.send_message(chat_id, f"{head}\n{body}", parse_mode=ParseMode.HTML)
    await set_turn_timer(context, chat_id)

# ====== COMMANDS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot đối chữ (rhyme) – có kiểm tra nghĩa bằng từ điển offline.\n"
        f"⌛ Thời gian/lượt: {ROUND_SECONDS}s • Tối thiểu: {MIN_WORD_LEN}\n"
        "Lệnh: /newgame, /join, /begin, /stop"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    MATCHES[chat_id] = Match()
    await update.message.reply_text("🧩 Tạo sảnh mới. Mọi người dùng /join để tham gia.")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    match = get_match(chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy, đợi ván sau nhé.")
        return
    if user_id in match.players:
        await update.message.reply_text("Bạn đã tham gia rồi!")
        return
    match.players.append(user_id)
    await update.message.reply_text(f"✅ {update.effective_user.full_name} đã tham gia. "
                                    f"Đang có {len(match.players)} người.")

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
    await update.message.reply_text("⏹️ Đã kết thúc ván hiện tại.")

# ====== XỬ LÝ TIN NHẮN NGƯỜI CHƠI ======
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

    # Kiểm tra độ dài tối thiểu
    if len(text) < MIN_WORD_LEN:
        await update.message.reply_text(f"❌ Từ quá ngắn (tối thiểu {MIN_WORD_LEN}). Bạn bị loại.")
        match.alive.pop(match.turn_idx)
    else:
        ok = True
        # 1) Kiểm tra có nghĩa theo từ điển
        if not is_valid_dictionary_word(text):
            ok = False
        # 2) Kiểm tra vần
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
            await update.message.reply_text("✅ Hợp lệ. Tới lượt người kế tiếp!")
        else:
            await update.message.reply_text("❌ Sai luật hoặc không có trong từ điển. Bạn bị loại.")
            match.alive.pop(match.turn_idx)

    # Kết thúc?
    if len(match.alive) == 1:
        win_id = match.alive[0]
        mem = await context.bot.get_chat_member(chat_id, win_id)
        await context.bot.send_message(chat_id, f"🏆 {mem.user.full_name} thắng! 🎉")
        match.active = False
        match.timer_job_id = None
        return

    match.turn_idx %= len(match.alive)
    await announce_turn(context, chat_id, match)

# ====== KHỞI TẠO APP ======
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
