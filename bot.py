import os
import re
import random
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ================== CONFIG ==================
TOKEN = os.getenv("TELEGRAM_TOKEN")
DICT_FILE = os.getenv("DICT_FILE", "dict_vi.txt")

ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", 60))   # thời gian 1 lượt
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", 30))  # cảnh báo giữa giờ

# ============================================

# ==== Load Dictionary (2 âm tiết có nghĩa) ====
def load_vi_dict(path: str) -> set[str]:
    s = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip().lower()
                if w and not w.startswith("#"):
                    s.add(w)
    except Exception as e:
        print(f"[DICT] Cannot load dictionary {path}: {e}")
    return s

VI_PHRASES = load_vi_dict(DICT_FILE)
print(f"[DICT] Loaded {len(VI_PHRASES)} entries from {DICT_FILE}")

# ====== Helper functions ======
_VI_KEEP = re.compile(r"[^0-9A-Za-zÀ-ỿà-ỹ\s]")

def normalize_vi(text: str) -> str:
    txt = _VI_KEEP.sub(" ", text).lower()
    return " ".join(txt.split())

def count_syllables(text: str) -> int:
    norm = normalize_vi(text)
    if not norm:
        return 0
    return len(norm.split())

def phrase_is_valid(text: str) -> tuple[bool, str, str]:
    norm = normalize_vi(text)
    if count_syllables(norm) != 2:
        return False, "syllable", norm
    if norm not in VI_PHRASES:
        return False, "dict", norm
    return True, "", norm

# ====== Game State ======
games = {}  # chat_id -> {players, turn_idx, alive, current_word}

# ====== Command Handlers ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Xin chào! Gõ /newgame để mở sảnh chơi.")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    games[chat_id] = {
        "players": [],
        "alive": [],
        "turn_idx": 0,
        "current_word": None,
    }
    await update.message.reply_text("🎮 Sảnh mở! Gõ /join để tham gia. Nếu không ai /begin thì sau 60s game sẽ tự bắt đầu.")

    # Auto begin sau 60s
    async def auto_begin(ctx: ContextTypes.DEFAULT_TYPE):
        if chat_id not in games:
            return
        g = games[chat_id]
        if not g["players"]:
            await ctx.bot.send_message(chat_id, "⏰ Không có ai tham gia, huỷ game.")
            return
        await begin_game(chat_id, ctx)

    context.job_queue.run_once(auto_begin, when=60, chat_id=chat_id, name=f"auto-{chat_id}")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("❌ Chưa có sảnh. Gõ /newgame trước.")
        return
    user = update.effective_user
    g = games[chat_id]
    if user.id not in g["players"]:
        g["players"].append(user.id)
        g["alive"].append(user.id)
        await update.message.reply_text(f"➕ {user.full_name} đã tham gia!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("❌ Chưa có sảnh. Gõ /newgame trước.")
        return
    await begin_game(chat_id, context)

# ====== Game Logic ======
async def begin_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games[chat_id]
    if not g["players"]:
        await context.bot.send_message(chat_id, "❌ Không có ai tham gia.")
        return
    g["turn_idx"] = random.randrange(len(g["alive"]))
    await context.bot.send_message(chat_id, "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    await announce_turn(chat_id, context)

async def announce_turn(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games[chat_id]
    if not g["alive"]:
        await context.bot.send_message(chat_id, "🏁 Không còn ai chơi, game kết thúc.")
        return
    uid = g["alive"][g["turn_idx"]]
    member = await context.bot.get_chat_member(chat_id, uid)
    await context.bot.send_message(
        chat_id,
        f"📖 Luật: đúng 2 từ • mỗi từ ≥2 ký tự • phải có nghĩa (nằm trong từ điển).\n"
        f"👉 {member.user.full_name} đi trước. Gửi cụm hợp lệ bất kỳ."
    )

    # setup timer jobs
    turn_name = f"turn-{chat_id}"
    half_name = f"half-{chat_id}"
    for name in (turn_name, half_name):
        for job in context.job_queue.get_jobs_by_name(name) or []:
            job.schedule_removal()

    HALF_LINES = [
        "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
        "Tắc ẻ đến vậy sao , 30 giây cuối nè :||",
        "30 vẫn chưa phải tết , nhưng mi sắp hết giờ rồi . 30 giây",
        "mắc đitt rặn mẵ không ra . 30 giây cuối ẻ",
        "30 giây cuối ní ơi",
    ]

    async def half_warn(ctx):
        await ctx.bot.send_message(chat_id, random.choice(HALF_LINES))

    async def timeout_cb(ctx):
        uid2 = g["alive"][g["turn_idx"]]
        member2 = await ctx.bot.get_chat_member(chat_id, uid2)
        await ctx.bot.send_message(chat_id, f"⏰ Hết giờ , mời {member2.user.full_name} ra ngoài chờ !!")
        g["alive"].pop(g["turn_idx"])
        if g["turn_idx"] >= len(g["alive"]):
            g["turn_idx"] = 0
        await announce_turn(chat_id, ctx)

    context.job_queue.run_once(half_warn, when=HALFTIME_SECONDS, name=half_name)
    context.job_queue.run_once(timeout_cb, when=ROUND_SECONDS, name=turn_name)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        return
    g = games[chat_id]
    if not g["alive"]:
        return
    uid = g["alive"][g["turn_idx"]]
    if update.effective_user.id != uid:
        return

    text_in = update.message.text or ""
    ok, reason, norm = phrase_is_valid(text_in)

    if not ok:
        if reason == "syllable":
            await update.message.reply_text("❌ 傻逼 Cấm Cãi !!!")
        else:
            WRONG_LINES = [
                "IQ bạn cần phải xem xét lại , mời tiếp !!",
                "Mỗi thế cũng sai , GG cũng không cứu được !",
                "Sai rồi má , Tra lại từ điển đi !",
                "Từ gì vậy má , Học lại lớp 1 đi !!",
                "Ảo tiếng Việt hee",
                "Loại , người tiếp theo",
                "Chưa tiến hoá hết à , từ này con người dùng sao . Sai bét!!",
            ]
            await update.message.reply_text("❌ " + random.choice(WRONG_LINES))
        g["alive"].pop(g["turn_idx"])
        if g["turn_idx"] >= len(g["alive"]):
            g["turn_idx"] = 0
        await announce_turn(chat_id, context)
        return

    # hợp lệ
    g["current_word"] = norm
    await update.message.reply_text(f"✅ Hợp lệ. Tới lượt kế tiếp!")
    g["turn_idx"] = (g["turn_idx"] + 1) % len(g["alive"])
    await announce_turn(chat_id, context)

# ====== Main ======
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()
