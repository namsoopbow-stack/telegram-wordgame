import os, re, time, unicodedata, asyncio, aiosqlite
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Set, Dict, Tuple

from rapidfuzz import fuzz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ===== Cấu hình =====
DB_PATH = os.environ.get("DB_PATH", "knockout.db")
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))   # 1 phút/lượt
MIN_WORD_LEN = int(os.environ.get("MIN_WORD_LEN", "2"))      # tối thiểu 2 ký tự
FUZZ_OK = int(os.environ.get("FUZZ_OK", "90"))               # ngưỡng khớp từ
DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "rhyme")       # mặc định rhyme

VN_BASE_MAP = str.maketrans({"đ":"d","Đ":"D"})
def vn_strip(s:str)->str:
    s = s.translate(VN_BASE_MAP)
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

def normalize_word(w:str)->str:
    w = re.sub(r"[^a-zA-Zà-ỹđĐ\s-]", "", (w or "").strip().lower())
    w = re.sub(r"\s+", " ", w).strip()
    return w.split(" ")[0] if w else ""

def last_letter(w:str)->str:
    s = vn_strip(normalize_word(w))
    return s[-1] if s else ""

# ===== Wordlist demo (nên thay bằng bộ lớn để chơi hay hơn) =====
VOCAB = {
  "hoa","anh","em","yeu","thuong","thu","thuat","nha","nhac","nhan","nguoi","nuoc",
  "gio","gioi","thoi","tho","dien","vui","ve","vang","gao","ong","mua","an",
  "ngon","ao","oai","im","man","nang","sang","toi","tim","ca","co","cu","ke",
  "kho","khue","quy","quen","gai","ga","go","gu","ban","bao","bong","bo","be",
  "bua","biet","lon","long","lau","lam","lan","lat","leo","lua","tinh","tien",
  "tieng","viet","vua","van"
}

def vocab_has(word:str)->bool:
    base = vn_strip(word)
    best = 0
    for v in VOCAB:
        if vn_strip(v) == base:
            return True
        best = max(best, fuzz.ratio(base, vn_strip(v)))
    return best >= FUZZ_OK

# ===== Trạng thái trận =====
@dataclass
class Match:
    active: bool = False
    mode: str = DEFAULT_MODE
    current: str = ""
    turn_idx: int = 0
    alive: deque = field(default_factory=deque)
    used: Set[str] = field(default_factory=set)
    last_player: Optional[int] = None

# ===== DB =====
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS winners(chat_id INTEGER, user_id INTEGER, won_at INTEGER);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def save_winner(chat_id:int, user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO winners(chat_id,user_id,won_at) VALUES(?,?,?)",
                         (chat_id, user_id, int(time.time())))
        await db.commit()

# ===== App =====
async def build_application(token:str)->Application:
    await init_db()
    app = ApplicationBuilder().token(token).build()
    app.bot_data["matches"]: Dict[int, Match] = {}
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

def get_match(context: ContextTypes.DEFAULT_TYPE, chat_id:int)->Match:
    matches = context.bot_data["matches"]
    if chat_id not in matches:
        matches[chat_id] = Match()
    return matches[chat_id]

# ===== Commands =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot đối chữ loại trực tiếp:\n"
        "• /newgame – mở sảnh\n"
        "• /join – tham gia\n"
        "• /begin – bắt đầu\n"
        f"Luật: {DEFAULT_MODE}, {ROUND_SECONDS}s/lượt, tối thiểu {MIN_WORD_LEN} ký tự."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(context, chat_id)
    match.active = False
    match.current = ""
    match.alive.clear()
    match.used.clear()
    match.turn_idx = 0
    await update.message.reply_text(
        "🎮 Mở sảnh mới! /join để tham gia, /begin để bắt đầu."
    )

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    match = get_match(context, chat_id)
    if match.active:
        await update.message.reply_text("Trận đang chạy.")
        return
    if user_id in match.alive:
        await update.message.reply_text("Bạn đã tham gia rồi.")
        return
    match.alive.append(user_id)
    await update.message.reply_text("Đã tham gia sảnh!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    match = get_match(context, chat_id)
    if len(match.alive) < 2:
        await update.message.reply_text("Cần ít nhất 2 người.")
        return
    match.active = True
    match.current = ""
    match.used.clear()
    match.turn_idx = 0
    await update.message.reply_text(
        "Bắt đầu! Người đầu tiên gửi từ hợp lệ."
    )

# ===== Gameplay =====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    match = get_match(context, chat_id)
    if not match.active or not match.alive:
        return

    if match.alive[match.turn_idx] != user_id:
        return  # không phải lượt của bạn

    w = normalize_word(text)
    if len(vn_strip(w)) < MIN_WORD_LEN or not vocab_has(w) or w in match.used:
        await update.message.reply_text("❌ Sai luật, bạn bị loại!")
        match.alive.remove(user_id)
        if len(match.alive) == 1:
            winner = match.alive[0]
            member = await context.bot.get_chat_member(chat_id, winner)
            name = member.user.full_name
            await save_winner(chat_id, winner)
            await update.message.reply_text(f"🏆 {name} thắng! 🎉")
            match.active = False
        else:
            match.turn_idx %= len(match.alive)
        return

    # hợp lệ
    match.current = w
    match.used.add(w)
    match.turn_idx = (match.turn_idx + 1) % len(match.alive)
    await update.message.reply_text(f"✅ Chấp nhận: {w}")

# ===== Polling local =====
async def _polling():
    token = os.environ["TELEGRAM_TOKEN"]
    app = await build_application(token)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.wait()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(_polling())
