# bot.py
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ====== CẤU HÌNH TỪ BIẾN MÔI TRƯỜNG ======
TOKEN = os.environ["TELEGRAM_TOKEN"]
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "20"))   # bạn đã đặt 60
MIN_WORD_LEN   = int(os.environ.get("MIN_WORD_LEN", "3"))    # bạn đã đặt 2
DEFAULT_MODE   = os.environ.get("DEFAULT_MODE", "rhyme")     # bạn đã đặt rhyme

# ====== TIỆN ÍCH XỬ LÝ “VẦN” ======
_VOWELS = "aăâeêiîoôơuưyAĂÂEÊIÎOÔƠUƯY"

def strip_diacritics(s: str) -> str:
    nf = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nf if unicodedata.category(ch) != "Mn")

def rhyme_key(word: str) -> str:
    """
    Khóa vần đơn giản: lấy 2 ký tự cuối của từ (bỏ dấu, bỏ khoảng trắng).
    Dùng cho luật 'rhyme' – đủ tốt để chơi vui trong nhóm.
    """
    w = strip_diacritics(word.strip().lower())
    # tách từ cuối cùng nếu là cụm nhiều từ
    last = w.split()[-1] if w else ""
    return last[-2:] if len(last) >= 2 else last

# ====== TRẠNG THÁI TRẬN ======
@dataclass
class Match:
    mode: str = DEFAULT_MODE              # chỉ dùng 'rhyme'
    active: bool = False
    players: List[int] = field(default_factory=list)  # tất cả người đã /join
    alive: List[int] = field(default_factory=list)    # đang còn trong ván
    turn_idx: int = 0
    current_word: str = ""                # từ trước đó
    used: Set[str] = field(default_factory=set)       # tránh lặp
    timer_job_id: Optional[str] = None

# Lưu trữ theo chat_id
MATCHES: Dict[int, Match] = {}

def get_match(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Match:
    if chat_id not in MATCHES:
        MATCHES[chat_id] = Match()
    return MATCHES[chat_id]

# ====== QUẢN LÝ LƯỢT & HẾT GIỜ ======
async def announce_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    chat_id = update.effective_chat.id
    user_id = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, user_id)
    prefix = f"🔁 Luật: vần (tối thiểu {MIN_WORD_LEN} ký tự)."
    if match.current_word:
        await context.bot.send_message(
            chat_id, f"{prefix}\n👉 {member.user.mention_html()} đến lượt!\n"
                     f"Từ trước: <b>{match.current_word}</b> (hãy gửi từ có vần giống)",
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id, f"{prefix}\n👉 {member.user.mention_html()} đi trước. Gửi bất kỳ từ hợp lệ.",
            parse_mode=ParseMode.HTML
        )

    # đặt hẹn giờ
    await set_turn_timer(context, chat_id)

async def set_turn_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = get_match(context, chat_id)
    # hủy job cũ
    if match.timer_job_id:
        old = context.job_queue.get_jobs_by_name(match.timer_job_id)
        for j in old: j.schedule_removal()
    # tạo job mới
    job_name = f"turn_{chat_id}"
    match.timer_job_id = job_name
    context.job_queue.run_once(timeout_eliminate, when=ROUND_SECONDS, name=job_name, data={"chat_id": chat_id})

async def timeout_eliminate(ctx):
    chat_id = ctx.job.data["chat_id"]
    context: ContextTypes.DEFAULT_TYPE = ctx.application  # type: ignore
    match = get_match(context, chat_id)
    if not match.active or not match.alive:
        return
    uid = match.alive[match.turn_idx]
    member = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(chat_id, f"⏰ Hết {ROUND_SECONDS}s – {member.user.mention_html()} bị loại!",
                                   parse_mode=ParseMode.HTML)
    # loại & kiểm tra thắng
    match.alive.pop(match.turn_idx)
    if len(match.alive) == 1:
        winner = match.alive[0]
        win_member = await context.bot.get_chat_member(chat_id, winner)
        await context.bot.send_message(chat_id, f"🏆 {win_member.user.full_name} thắng! 🎉")
        match.active = False
        match.timer_job_id = None
        return
    match.turn_idx %= len(match.alive)
    await announce_turn(await context.bot.get_chat(chat_id), context, match)  # type: ignore

# ====== COMMANDS CƠ BẢN ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot đối chữ đã sẵn sàng!\n"
                                    "Lệnh: /newgame, /join, /begin, /stop\n"
                                    f"Luật: vần • lượt: {ROUND_SECONDS}s • tối thiểu {MIN_WORD_LEN} ký tự")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    MATCHES[chat_id] = Match()  # reset
    await update.message.reply_text("🧩 Tạo sảnh mới. Mọi người dùng /join để tham gia.")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    match = get_match(context, chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy, đợi ván sau nhé.")
        return
    if user_id in match.players:
        await update.message.reply_text("Bạn đã tham gia rồi!")
        return
    match.players.append(user_id)
    await update.message.reply_text(f"✅ {update.effective_user.full_name} đã tham gia. "
                                    f"Hiện có {len(match.players)} người.")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(context, chat_id)
    if match.active:
        await update.message.reply_text("Ván đang chạy rồi.")
        return
    if len(match.players) < 2:
        await update.message.reply_text("Cần ít nhất 2 người /join mới bắt đầu được.")
        return
    match.active = True
    match.alive = list(match.players)
    match.turn_idx = 0
    match.current_word = ""
    match.used.clear()
    await update.message.reply_text("🚀 Bắt đầu! Loại trực tiếp: sai hoặc hết giờ là rời bàn.")
    await announce_turn(update, context, match)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(context, chat_id)
    match.active = False
    match.timer_job_id = None
    await update.message.reply_text("⏹️ Đã kết thúc ván hiện tại.")

# ====== XỬ LÝ TIN NHẮN NGƯỜI CHƠI ======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text.strip()

    match = get_match(context, chat_id)
    if not match.active or not match.alive:
        return  # bỏ qua khi chưa chơi

    # chỉ người đến lượt được nói
    if user_id != match.alive[match.turn_idx]:
        return

    # kiểm tra tối thiểu ký tự
    if len(text) < MIN_WORD_LEN:
        await update.message.reply_text(f"❌ Từ quá ngắn (tối thiểu {MIN_WORD_LEN}). Bạn bị loại.")
        match.alive.pop(match.turn_idx)
    else:
        # luật rhyme
        ok = True
        if match.current_word:
            ok = rhyme_key(text) != "" and rhyme_key(text) == rhyme_key(match.current_word)
        # không lặp lại từ đã dùng (không bắt buộc nhưng hay hơn)
        key = strip_diacritics(text.lower())
        if key in match.used:
            ok = False
        if ok:
            match.used.add(key)
            match.current_word = text
            # chuyển lượt
            match.turn_idx = (match.turn_idx + 1) % len(match.alive)
            await update.message.reply_text(f"✅ Hợp lệ. Tới lượt người kế tiếp!")
        else:
            await update.message.reply_text("❌ Sai luật (không cùng vần hoặc lặp). Bạn bị loại.")
            match.alive.pop(match.turn_idx)

    # kiểm tra thắng/thua
    if len(match.alive) == 1:
        winner = match.alive[0]
        member = await context.bot.get_chat_member(chat_id, winner)
        await context.bot.send_message(chat_id, f"🏆 {member.user.full_name} thắng! 🎉")
        match.active = False
        match.timer_job_id = None
        return

    # chuyển lượt và đặt lại hẹn giờ
    match.turn_idx %= len(match.alive)
    await announce_turn(update, context, match)

# ====== KHỞI TẠO APP ======
def build_application() -> Application:
    app = Application.builder().token(TOKEN).build()
    # lệnh
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join",    cmd_join))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    # text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
