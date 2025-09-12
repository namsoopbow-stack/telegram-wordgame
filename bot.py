import os, random, asyncio
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List
from unidecode import unidecode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

# ===== Cấu hình =====
COUNTDOWN_SECONDS = int(os.getenv("COUNTDOWN_SECONDS", "60"))
TURN_SECONDS = int(os.getenv("TURN_SECONDS", "60"))  # mỗi lượt
VERB_DICT_FILE = os.getenv("VERB_DICT_FILE", "verbs_vi.txt")
PING_ALLOWED = os.getenv("PING_ALLOWED", "@yhck2").lower()
PING_SPECIAL_OK_USER = os.getenv("PING_SPECIAL_OK_USER", "@xiaoc6789").lower()

# ===== Kho câu nhắc/cà khịa =====
REMIND_LOBBY_30S = [
    "⏰ Cú lừa à? Không, chỉ còn **30s** để /join thôi!",
    "Nhanh tay nào, **30s** nữa là đóng sảnh!",
    "Vào nhanh kẻo lỡ chuyến, **30 giây cuối**!",
    "Thiệt hại miệng nói ít thôi, **30s** nữa là chơi!",
    "Đếm ngược kêu gọi đồng bọn: **30 giây**!",
    "Làm biếng là thua: **30s** cuối cùng!",
    "Sảnh sắp chốt, **30s** chót lót!",
    "Ai chưa /join thì vào liền, còn **30s**!",
    "Nhanh còn kịp, **30 giây** là hết phim!",
    "Đếm 3…2…1… à chưa, còn **30s** 😎",
]

REMIND_TURN_HALF = [
    "Nhanh nhanh lên bạn ơi, **thời gian không chờ ai**!",
    "Có đoán được không? **Chậm thế!**",
    "IQ chỉ tới đó thôi sao? **Nhanh cái não lên!**",
    "Suy nghĩ gì nữa! **Đánh điiii!**",
    "Vẫn chưa ra? **Não heo 🐷** thật à!",
    "Gõ lẹ đi, **nửa thời gian** bay rồi!",
    "Bình tĩnh mà không chậm nhé, **30s** cuối!",
    "Chơi chữ chứ không chơi đếm cát, **nhanh!**",
    "Đừng để BOT cười, **30 giây cuối cùng!**",
    "Trì hoãn là kẻ cắp thời gian đó nha!",
]

ELIMINATE_LINES = [
    "Loại! Luật rành mà làm sai là **xuống ghế**!",
    "Tạch! Về **chuồng động vật** ngồi cho ấm!",
    "Xin vĩnh biệt cụ, **out** vì sai luật!",
    "Ối dồi ôi… **loại** vì chơi bẩn (sai luật)!",
    "Gõ cho vui chứ không đúng luật thì **bye**!",
    "Bạn bị **đá** khỏi vòng vì phạm luật!",
    "Sẩy chân một cái là **ra rìa** liền!",
    "Không đúng quy chuẩn → **tạm biệt**!",
    "Sai một ly, đi **vài cây số** – loại!",
    "Thôi xong… **mời rời sân** vì sai luật!",
]

OK_LINES = [
    "Chuẩn bài! ✅",
    "Được đấy! Tiếp! ✅",
    "Ngon! Đi nhịp tiếp nào! ✅",
    "Hợp lệ. Đến bạn kế! ✅",
    "Đúng luật, mời người tiếp theo! ✅",
]

# ===== Tiện ích =====
def norm(s: str) -> str:
    s = unidecode((s or "").strip().lower())
    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
    return " ".join("".join(out).split())

def split_two_words(text: str) -> Optional[List[str]]:
    parts = norm(text).split()
    if len(parts) != 2: return None
    if len(parts[0]) < 2 or len(parts[1]) < 2: return None
    return parts

def load_verbs() -> Set[str]:
    verbs = set()
    try:
        with open(VERB_DICT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                w = norm(line)
                if w: verbs.add(w)
    except FileNotFoundError:
        pass
    return verbs

VERBS: Set[str] = load_verbs()

# ===== Trạng thái =====
@dataclass
class Game:
    waiting: bool = False
    countdown_job: Optional[str] = None
    countdown_half_job: Optional[str] = None
    joined: Set[int] = field(default_factory=set)
    usernames: Dict[int, str] = field(default_factory=dict)

    active: bool = False
    required_first: Optional[str] = None
    last_player: Optional[int] = None
    turn_deadline_job: Optional[str] = None
    turn_half_job: Optional[str] = None

GAMES: Dict[int, Game] = {}

# ===== Luật hợp lệ =====
def is_valid_phrase(phrase: str, required_first: Optional[str]) -> (bool, str, Optional[List[str]]):
    parts = split_two_words(phrase)
    if not parts:
        return False, "Sai định dạng: phải đúng 2 từ, mỗi từ ≥2 ký tự.", None
    a, b = parts
    if required_first and a != required_first:
        return False, f"Sai luật: từ đầu phải là **{required_first}**.", parts
    if a not in VERBS or b not in VERBS:
        return False, "Câu phải gồm **động từ** (chủ đề hành động).", parts
    return True, "OK", parts

# ===== BOT đi nước khi solo =====
def bot_move(required_first: str) -> str:
    choices = [v for v in VERBS if v != required_first] or [required_first]
    return f"{required_first} {random.choice(choices)}"

# ===== Helpers JobQueue =====
def cancel_job_by_name(context: ContextTypes.DEFAULT_TYPE, name: Optional[str]):
    if not name: return
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

def schedule_turn_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int, g: Game):
    # hủy cũ
    cancel_job_by_name(context, g.turn_half_job)
    cancel_job_by_name(context, g.turn_deadline_job)
    # half reminder
    if TURN_SECONDS >= 30:
        g.turn_half_job = f"half-{chat_id}-{random.randint(1,999999)}"
        context.job_queue.run_once(turn_half_remind, when=TURN_SECONDS//2, chat_id=chat_id, name=g.turn_half_job)
    # deadline
    g.turn_deadline_job = f"dead-{chat_id}-{random.randint(1,999999)}"
    context.job_queue.run_once(turn_timeout, when=TURN_SECONDS, chat_id=chat_id, name=g.turn_deadline_job)

# ===== Handlers =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh (đếm 60s và nhắc 30s), /join để tham gia, /ketthuc để dừng.\n"
        "Luật: câu phải có **đúng 2 từ**, **từ 1** của câu sau **trùng** **từ 2** của câu trước.\n"
        "Chỉ chấp nhận **động từ** (chủ đề hành động). Sai luật = **loại ngay**."
    )

def ensure_game(chat_id: int) -> Game:
    if chat_id not in GAMES: GAMES[chat_id] = Game()
    return GAMES[chat_id]

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return await update.effective_message.reply_text("Dùng trong nhóm nhé.")
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)

    # reset
    g.waiting = True; g.active = False
    g.joined.clear(); g.usernames.clear()
    g.required_first = None; g.last_player = None
    cancel_job_by_name(context, g.countdown_job)
    cancel_job_by_name(context, g.countdown_half_job)

    g.countdown_job = f"cd-{chat_id}-{random.randint(1,999999)}"
    context.job_queue.run_once(countdown_done, when=COUNTDOWN_SECONDS, chat_id=chat_id, name=g.countdown_job)
    if COUNTDOWN_SECONDS >= 30:
        g.countdown_half_job = f"cdh-{chat_id}-{random.randint(1,999999)}"
        context.job_queue.run_once(countdown_half, when=COUNTDOWN_SECONDS-30, chat_id=chat_id, name=g.countdown_half_job)

    await update.effective_message.reply_text(
        f"🟢 **Mở sảnh** – còn {COUNTDOWN_SECONDS}s để /join.\n"
        "• Không ai /join → **huỷ ván**\n"
        "• 1 người /join → **đấu với BOT**\n"
        "• ≥2 người → **các bạn tự đấu với nhau**", parse_mode="Markdown"
    )

async def countdown_half(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id, random.choice(REMIND_LOBBY_30S))

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    if not g.waiting:
        return await update.effective_message.reply_text("Chưa mở sảnh. Dùng /newgame trước nhé.")
    u = update.effective_user
    g.joined.add(u.id)
    g.usernames[u.id] = "@" + (u.username or str(u.id))
    await update.effective_message.reply_text(f"✅ {g.usernames[u.id]} đã tham gia! (hiện có {len(g.joined)} người)")

async def countdown_done(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    g = ensure_game(chat_id)
    g.countdown_job = None; g.countdown_half_job = None
    if not g.waiting: return

    if len(g.joined) == 0:
        g.waiting = False
        await context.bot.send_message(chat_id, "❌ Không ai tham gia. Huỷ ván.")
        return

    g.waiting = False; g.active = True; g.required_first = None; g.last_player = None
    players = [g.usernames.get(uid, str(uid)) for uid in g.joined]
    mode = "👤 1 người vs 🤖 BOT" if len(g.joined) == 1 else "👥 Nhiều người"
    await context.bot.send_message(chat_id, f"🚀 **Bắt đầu!** Chế độ: {mode}\nNgười chơi: {', '.join(players)}", parse_mode="Markdown")
    await context.bot.send_message(chat_id, "Gửi **2 động từ** bất kỳ để mở nhịp!")

    # khởi tạo đếm lượt đầu tiên (đợi cú mở nhịp)
    schedule_turn_jobs(context, chat_id, g)

async def turn_half_remind(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id, random.choice(REMIND_TURN_HALF))

async def turn_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    g = ensure_game(chat_id)
    # hết thời gian lượt → chỉ nhắc, không loại ai (tuỳ bạn muốn siết chặt thì có thể loại người vừa đặt nhịp)
    await context.bot.send_message(chat_id, "⏳ Hết thời gian lượt! Gửi câu mới để tiếp tục.")
    # bắt đầu lại bộ đếm lượt
    schedule_turn_jobs(context, chat_id, g)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    g.waiting = g.active = False
    g.joined.clear(); g.usernames.clear()
    g.required_first = None; g.last_player = None
    cancel_job_by_name(context, g.countdown_job)
    cancel_job_by_name(context, g.countdown_half_job)
    cancel_job_by_name(context, g.turn_half_job)
    cancel_job_by_name(context, g.turn_deadline_job)
    await update.effective_message.reply_text("⏹ Đã dừng ván.")

# —— ping (2 nút) ——
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = "@" + (user.username or "")
    if username.lower() != PING_ALLOWED:
        return await update.effective_message.reply_text("Lệnh này chỉ dành cho người đặc biệt 😉")
    kb = [[InlineKeyboardButton("Đồng ý", callback_data="ping:ok"),
           InlineKeyboardButton("Không",  callback_data="ping:no")]]
    await update.effective_message.reply_text(
        "Yêu Em Thiệu 🥰 Làm Người Yêu Anh Nhé !!!",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def on_ping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user = q.from_user
    username = "@" + (user.username or "")
    if username.lower() == PING_SPECIAL_OK_USER:
        text = "Em đồng ý !! Yêu Anh 🥰"
    else:
        text = "Thiệu ơi !! Yêu Anh Nam Đii"
    await q.message.reply_text(text)

# —— xử lý chat trong ván ——
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"): return
    chat_id = update.effective_chat.id
    g = ensure_game(chat_id)
    if not g.active: return

    text = update.effective_message.text or ""
    ok, reason, parts = is_valid_phrase(text, g.required_first)
    if not ok:
        u = update.effective_user
        uname = "@" + (u.username or str(u.id))
        if u.id in g.joined:
            g.joined.discard(u.id)
            await update.effective_message.reply_text(f"❌ {random.choice(ELIMINATE_LINES)}\nLý do: {reason}\n→ {uname} bị loại.")
            if len(g.joined) == 0:
                g.active = False
                await context.bot.send_message(chat_id, "⛔ Hết người chơi. Kết thúc ván.")
        else:
            await update.effective_message.reply_text(f"❌ Sai luật: {reason}")
        return

    # hợp lệ
    a, b = parts
    g.required_first = b
    g.last_player = update.effective_user.id
    await update.effective_message.reply_text(random.choice(OK_LINES))

    # reset đồng hồ lượt
    schedule_turn_jobs(context, chat_id, g)

    # solo vs BOT → bot đánh ngay
    if len(g.joined) == 1 and g.last_player in g.joined:
        bot_phrase = bot_move(b)
        await context.bot.send_message(chat_id, f"🤖 {bot_phrase}")
        _, next_b = split_two_words(bot_phrase)
        g.required_first = next_b
        schedule_turn_jobs(context, chat_id, g)

# ===== Build App =====
def build_app():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token: raise RuntimeError("Thiếu TELEGRAM_TOKEN")
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
    print("Run with webhook (see webhook.py)")
