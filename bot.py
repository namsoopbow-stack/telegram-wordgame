import os
import random
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List

from unidecode import unidecode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ================== Cấu hình ==================
COUNTDOWN_SECONDS = int(os.getenv("COUNTDOWN_SECONDS", "60"))
TURN_SECONDS = int(os.getenv("TURN_SECONDS", "60"))  # nếu muốn giới hạn mỗi lượt
VERB_DICT_FILE = os.getenv("VERB_DICT_FILE", "verbs_vi.txt")
PING_ALLOWED = os.getenv("PING_ALLOWED", "@yhck2").lower()
PING_SPECIAL_OK_USER = os.getenv("PING_SPECIAL_OK_USER", "@xiaoc6789").lower()

# ================== Tiện ích ==================
def norm(s: str) -> str:
    """chuẩn hóa: bỏ dấu, thường hóa, bỏ ký tự lạ, rút gọn khoảng trắng"""
    s = unidecode((s or "").strip().lower())
    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
    s = "".join(out)
    return " ".join(s.split())

def split_two_words(text: str) -> Optional[List[str]]:
    t = norm(text)
    parts = t.split()
    if len(parts) != 2:
        return None
    if len(parts[0]) < 2 or len(parts[1]) < 2:
        return None
    return parts

def load_verbs() -> Set[str]:
    verbs = set()
    try:
        with open(VERB_DICT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                w = norm(line)
                if w:
                    verbs.add(w)
    except FileNotFoundError:
        pass
    return verbs

VERBS: Set[str] = load_verbs()

# ================== Trạng thái ==================
@dataclass
class Game:
    waiting: bool = False                 # đang đếm ngược
    countdown_job: Optional[str] = None   # job id đếm ngược
    joined: Set[int] = field(default_factory=set)
    usernames: Dict[int, str] = field(default_factory=dict)

    active: bool = False
    required_first: Optional[str] = None  # từ 1 bắt buộc của câu kế tiếp (chính là từ 2 của câu trước)
    last_player: Optional[int] = None     # ai vừa trả lời hợp lệ
    turn_deadline_job: Optional[str] = None

GAMES: Dict[int, Game] = {}

# ================== Kiểm tra hợp lệ câu ==================
def is_valid_phrase(phrase: str, required_first: Optional[str]) -> (bool, str, Optional[List[str]]):
    parts = split_two_words(phrase)
    if not parts:
        return False, "Sai định dạng: phải là đúng 2 từ, mỗi từ ≥2 ký tự.", None

    a, b = parts

    # bắt buộc từ đầu = required_first nếu đang nối
    if required_first and a != required_first:
        return False, f"Sai luật: từ đầu phải là **{required_first}**.", parts

    # chỉ cho phép **động từ** (chủ đề hành động)
    if a not in VERBS or b not in VERBS:
        return False, "Câu phải gồm **động từ** (theo chủ đề hành động).", parts

    return True, "OK", parts

# ================== BOT đánh khi chỉ có 1 người ==================
def bot_move(required_first: str) -> str:
    # tìm 1 động từ khác ngẫu nhiên để ghép thành 2 từ
    # bảo đảm động từ thứ 2 khác để đỡ lặp nhàm
    choices = [v for v in VERBS if v != required_first]
    if not choices:
        # fallback: lặp lại cũng được (nhưng rất hiếm)
        second = required_first
    else:
        second = random.choice(choices)
    return f"{required_first} {second}"

# ================== Handlers ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia, /ketthuc để dừng.\n"
        "Luật: câu phải có **đúng 2 từ**, và **từ 1** của câu sau phải **trùng** với **từ 2** của câu trước.\n"
        "Chỉ chấp nhận **động từ** (chủ đề hành động). Sai luật là **loại ngay**."
    )

def ensure_game(chat_id: int) -> Game:
    game = GAMES.get(chat_id)
    if not game:
        game = Game()
        GAMES[chat_id] = game
    return game

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.effective_message.reply_text("Dùng trong nhóm nhé.")
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)

    # reset ván
    g.waiting = True
    g.active = False
    g.joined.clear()
    g.usernames.clear()
    g.required_first = None
    # hủy job cũ
    if g.countdown_job:
        for j in context.job_queue.get_jobs_by_name(g.countdown_job):
            j.schedule_removal()
    g.countdown_job = f"cd-{chat_id}-{random.randint(1,999999)}"
    context.job_queue.run_once(countdown_done, when=COUNTDOWN_SECONDS, chat_id=chat_id, name=g.countdown_job)

    await update.effective_message.reply_text(
        f"🟢 **Mở sảnh** – còn {COUNTDOWN_SECONDS}s để /join.\n"
        "• Không ai /join → **huỷ ván**\n"
        "• 1 người /join → **đấu với BOT**\n"
        "• ≥2 người → **các bạn tự đấu với nhau**",
        parse_mode="Markdown"
    )

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    if not g.waiting:
        return await update.effective_message.reply_text("Chưa mở sảnh. Dùng /newgame trước nhé.")

    user = update.effective_user
    g.joined.add(user.id)
    g.usernames[user.id] = "@" + (user.username or str(user.id))
    await update.effective_message.reply_text(f"✅ {g.usernames[user.id]} đã tham gia! (hiện có {len(g.joined)} người)")

async def countdown_done(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    g = ensure_game(chat_id)
    g.countdown_job = None
    if not g.waiting:
        return

    if len(g.joined) == 0:
        g.waiting = False
        await context.bot.send_message(chat_id, "❌ Không ai tham gia. Huỷ ván.")
        return

    g.waiting = False
    g.active = True
    g.required_first = None
    players = [g.usernames.get(uid, str(uid)) for uid in g.joined]
    mode = "👤 1 người vs 🤖 BOT" if len(g.joined) == 1 else "👥 Nhiều người"
    await context.bot.send_message(chat_id, f"🚀 **Bắt đầu!** Chế độ: {mode}\nNgười chơi: {', '.join(players)}", parse_mode="Markdown")

    # Ai cũng có thể trả lời; bắt đầu chưa có required_first → ai gửi câu hợp lệ đầu tiên sẽ đặt nhịp.
    await context.bot.send_message(chat_id, "Gửi **2 từ (động từ)** bất kỳ để mở nhịp!")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    g.waiting = False
    g.active = False
    g.joined.clear()
    g.usernames.clear()
    g.required_first = None
    if g.countdown_job:
        for j in context.job_queue.get_jobs_by_name(g.countdown_job):
            j.schedule_removal()
        g.countdown_job = None
    await update.effective_message.reply_text("⏹ Đã dừng ván.")

# ====== /ping – nút bấm ======
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = "@" + (user.username or "")
    if username.lower() != PING_ALLOWED:
        return await update.effective_message.reply_text("Lệnh này chỉ dành cho người đặc biệt 😉")
    kb = [
        [InlineKeyboardButton("Đồng ý", callback_data="ping:ok"),
         InlineKeyboardButton("Không", callback_data="ping:no")]
    ]
    await update.effective_message.reply_text(
        "Yêu Em Thiệu 🥰 Làm Người Yêu Anh Nhé !!!",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def on_ping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    username = "@" + (user.username or "")
    if username.lower() == PING_SPECIAL_OK_USER:
        text = "Em đồng ý !! Yêu Anh 🥰"
    else:
        text = "Thiệu ơi !! Yêu Anh Nam Đii"
    await q.message.reply_text(text)

# ====== Xử lý chat trong ván ======
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    if not g.active:
        return

    text = update.effective_message.text or ""
    ok, reason, parts = is_valid_phrase(text, g.required_first)
    if not ok:
        # loại ngay người phạm luật nếu đang là người chơi được tính (ở đây loại khỏi set joined)
        u = update.effective_user
        uname = "@" + (u.username or str(u.id))
        if u.id in g.joined:
            g.joined.discard(u.id)
            await update.effective_message.reply_text(f"❌ {uname} bị loại: {reason}")
        else:
            await update.effective_message.reply_text(f"❌ Sai luật: {reason}")
        # nếu chỉ còn 0 người thì dừng
        if g.active and len(g.joined) == 0:
            g.active = False
            await context.bot.send_message(chat_id, "⛔ Hết người chơi. Kết thúc ván.")
        return

    # câu hợp lệ
    a, b = parts
    g.required_first = b  # từ 2 của câu này sẽ là từ 1 của câu tiếp theo
    g.last_player = update.effective_user.id
    await update.effective_message.reply_text("✅ Hợp lệ. Tiếp đi nào!")

    # Nếu chế độ 1 người vs BOT → bot đánh ngay
    if len(g.joined) == 1 and g.last_player in g.joined:
        # bot phải gửi câu có từ đầu = b
        bot_phrase = bot_move(b)
        await context.bot.send_message(chat_id, f"🤖 {bot_phrase}")
        # cập nhật required cho lượt người tiếp theo
        _, next_b = split_two_words(bot_phrase)
        g.required_first = next_b

# ================== Build app ==================
def build_app() -> Application:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("join", cmd_join, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("ketthuc", cmd_stop, filters=filters.ChatType.GROUPS))

    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CallbackQueryHandler(on_ping_button, pattern=r"^ping:(ok|no)$"))

    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, on_message))
    return app

if __name__ == "__main__":
    print("Run with webhook (see webhook.py).")
