# -*- coding: utf-8 -*-
"""
Wordgame Bot – phiên bản tích hợp:
- /newgame: mở sảnh, auto-begin sau 60s nếu không ai gõ /begin
- /join: tham gia
- /begin: bắt đầu ngay (nếu muốn)
- Luật: đúng 2 từ, mỗi từ >= 2 ký tự, đều có trong từ điển (dict_vi.txt).
- Mỗi lượt 60s; sau 30s nếu người chơi chưa trả lời sẽ nhắc; hết 60s mà chưa trả lời -> LOẠI.
- Trả lời sai -> LOẠI.
"""

import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Request  # webhook
from telegram import Update, Message, User
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    ContextTypes, MessageHandler, filters
)

# ====== CẤU HÌNH ======
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))   # mặc định 60s
MIN_WORD_LEN = int(os.environ.get("MIN_WORD_LEN", "2"))      # mỗi từ >=2 ký tự
AUTO_BEGIN_SECONDS = int(os.environ.get("AUTO_BEGIN_SECONDS", "60"))  # 60s auto-begin
DICT_PATH = os.environ.get("DICT_PATH", "dict_vi.txt")       # đường dẫn file từ điển

# ====== TỪ/CÂU MẪU ======
HALF_WARNINGS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời :))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30 vẫn chưa phải tết, nhưng mi sắp hết giờ rồi. 30 giây!",
    "Mắc đít rặn mãi không ra… 30 giây cuối nè!",
    "30 giây cuối ní ơi!"
]

WRONG_MESSAGES = [
    "IQ bạn cần phải xem xét lại, mời tiếp!!",
    "Mỗi thế cũng sai, GG cũng không cứu được!",
    "Sai rồi má, tra lại từ điển đi!",
    "Từ gì vậy má, học lại lớp 1 đi!!",
    "Ảo tiếng Việt hả hee?",
    "Loại! Người tiếp theo.",
    "Chưa tiến hoá hết à? Từ này con người dùng sao… Sai bét!!"
]

TIMEOUT_MESSAGE = "⏰ Hết giờ, mời bạn ra ngoài chờ!!"

RULES_TEXT = (
    "📘 Luật: đúng 2 từ • mỗi từ ≥2 ký tự • mỗi từ phải có nghĩa (nằm trong từ điển)."
)

# ====== TỪ ĐIỂN: nạp từ file (mỗi dòng 1 từ, chữ thường) ======
def load_dictionary(path: str) -> Set[str]:
    words: Set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip().lower()
                if not w:
                    continue
                # bỏ dấu chấm phẩy, số… giữ chữ và khoảng trắng trong từ đơn
                if re.fullmatch(r"[0-9\s\W]+", w):
                    continue
                words.add(w)
        if words:
            print(f"[DICT] Loaded {len(words)} words from {path}")
            return words
    except FileNotFoundError:
        pass

    # Fallback seed (ít) để chạy tạm nếu bạn chưa upload dict_vi.txt
    seed = """
    xinh xắn
    hiền hậu
    mạnh mẽ
    hoa hồng
    hoa cúc
    bánh mì
    nước mía
    cà phê
    bờ biển
    trường học
    công viên
    bầu trời
    mặt trăng
    con mèo
    con chó
    """.strip().splitlines()
    for w in seed:
        words.add(w.strip().lower())
    print(f"[DICT] Fallback seed used: {len(words)} words")
    return words

VN_DICT: Set[str] = load_dictionary(DICT_PATH)

# ====== TRẠNG THÁI VÁN ======
@dataclass
class Match:
    chat_id: int
    players: List[int] = field(default_factory=list)   # danh sách id user đã join
    alive: List[int] = field(default_factory=list)     # id còn sống
    active: bool = False
    turn_idx: int = 0
    current_phrase: Optional[str] = None               # cụm 2 từ hợp lệ gần nhất
    used: Set[str] = field(default_factory=set)        # các cụm đã dùng (lower)
    # timer:
    start_ts: float = 0.0                              # thời điểm bắt đầu lượt
    half_job_name: Optional[str] = None
    timeout_job_name: Optional[str] = None
    auto_begin_job_name: Optional[str] = None
    # cờ: đã gõ gì trong lượt chưa (để 30s mới nhắc)
    spoke_in_turn: bool = False

matches: Dict[int, Match] = {}  # chat_id -> Match

# ====== TIỆN ÍCH ======
def mention_html(user: User) -> str:
    name = (user.full_name or user.username or str(user.id))
    return f"<a href=\"tg://user?id={user.id}\">{name}</a>"

def is_valid_phrase(text: str) -> bool:
    """
    Đúng 2 từ, mỗi từ >=2 ký tự, và MỖI TỪ đều tồn tại trong từ điển VN_DICT.
    So khớp theo chữ thường, giữ nguyên dấu tiếng Việt.
    """
    if not text:
        return False
    norm = " ".join(text.strip().split())  # gọn khoảng trắng
    parts = norm.split(" ")
    if len(parts) != 2:
        return False
    for p in parts:
        if len(p) < MIN_WORD_LEN:
            return False
        if p.lower() not in VN_DICT:
            return False
    return True

def pick_first_turn(match: Match, context: ContextTypes.DEFAULT_TYPE) -> None:
    random.shuffle(match.alive)
    match.turn_idx = 0
    match.spoke_in_turn = False
    match.current_phrase = None

def curr_player_id(match: Match) -> int:
    return match.alive[match.turn_idx % len(match.alive)]

async def send_rules(context: ContextTypes.DEFAULT_TYPE, chat_id: int, who_first: User):
    await context.bot.send_message(
        chat_id,
        f"{RULES_TEXT}\n\n👉 {mention_html(who_first)} đi trước. Gửi cụm hợp lệ bất kỳ.",
        parse_mode=ParseMode.HTML
    )

async def announce_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = matches[chat_id]
    uid = curr_player_id(match)
    member = await context.bot.get_chat_member(chat_id, uid)
    if match.current_phrase:
        await context.bot.send_message(
            chat_id,
            f"🔁 {mention_html(member.user)} đến lượt! "
            f"→ Gửi **cụm 2 từ** có nghĩa (không trùng).",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"🟢 {mention_html(member.user)} đi trước. Gửi **cụm 2 từ** có nghĩa bất kỳ.",
            parse_mode=ParseMode.HTML
        )
    match.spoke_in_turn = False
    await set_turn_timers(context, chat_id)

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    match = matches[chat_id]
    # huỷ job cũ
    await cancel_job_by_name(context, match.half_job_name)
    await cancel_job_by_name(context, match.timeout_job_name)

    half_name = f"half_{chat_id}"
    to_name = f"timeout_{chat_id}"
    match.half_job_name = half_name
    match.timeout_job_name = to_name

    context.job_queue.run_once(half_warn_cb, ROUND_SECONDS // 2, chat_id=chat_id, name=half_name)
    context.job_queue.run_once(timeout_cb, ROUND_SECONDS, chat_id=chat_id, name=to_name)

async def cancel_job_by_name(context: ContextTypes.DEFAULT_TYPE, name: Optional[str]):
    if not name:
        return
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

# 30s cảnh báo
async def half_warn_cb(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    match = matches.get(chat_id)
    if not match or not match.active:
        return
    if not match.spoke_in_turn:
        await context.bot.send_message(chat_id, "⚠️ " + random.choice(HALF_WARNINGS))

# 60s hết giờ -> loại
async def timeout_cb(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    match = matches.get(chat_id)
    if not match or not match.active:
        return
    # nếu tới đây mà vẫn chưa nói ra cụm hợp lệ => loại
    await eliminate_current_player(context, chat_id, by_timeout=True)

async def eliminate_current_player(context: ContextTypes.DEFAULT_TYPE, chat_id: int, by_timeout: bool = False):
    match = matches.get(chat_id)
    if not match or not match.active:
        return
    uid = curr_player_id(match)
    member = await context.bot.get_chat_member(chat_id, uid)
    msg = TIMEOUT_MESSAGE if by_timeout else random.choice(WRONG_MESSAGES)
    await context.bot.send_message(chat_id, f"❌ {mention_html(member.user)}: {msg}", parse_mode=ParseMode.HTML)

    # loại người chơi
    match.alive.pop(match.turn_idx % len(match.alive))
    # kết thúc?
    if len(match.alive) == 1:
        winner = match.alive[0]
        member = await context.bot.get_chat_member(chat_id, winner)
        await context.bot.send_message(
            chat_id,
            f"🏆 {mention_html(member.user)} chiến thắng! Chúc mừng 🎉",
            parse_mode=ParseMode.HTML
        )
        match.active = False
        await cancel_job_by_name(context, match.half_job_name)
        await cancel_job_by_name(context, match.timeout_job_name)
        return

    # chuyển lượt (không tăng turn_idx vì đã pop phần tử hiện tại)
    match.spoke_in_turn = False
    await announce_turn(context, chat_id)

# ====== LỆNH ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Xin chào! Dùng /newgame để mở sảnh, /join để tham gia, /begin để bắt đầu ngay.\n" +
        RULES_TEXT
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pong!")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id)
    if m and m.active:
        await update.message.reply_text("⚠️ Ván hiện tại đang chạy.")
        return

    match = Match(chat_id=chat_id)
    matches[chat_id] = match
    await update.message.reply_text(
        "🎮 Sảnh mở! /join để tham gia. Không ai /begin thì 60s nữa tự bắt đầu."
    )

    # Auto-begin sau 60s
    name = f"auto_begin_{chat_id}"
    match.auto_begin_job_name = name
    context.job_queue.run_once(auto_begin_cb, AUTO_BEGIN_SECONDS, chat_id=chat_id, name=name)

async def auto_begin_cb(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    match = matches.get(chat_id)
    if not match or match.active:
        return
    # nếu có ít nhất 1 người thì bắt
    if not match.players:
        await context.bot.send_message(chat_id, "⛔ Không ai tham gia, hủy sảnh.")
        matches.pop(chat_id, None)
        return
    await cmd_begin(None, context, chat_id=chat_id)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    match = matches.get(chat_id)
    if not match:
        await update.message.reply_text("❗ Chưa mở sảnh. Dùng /newgame trước.")
        return
    if user.id in match.players:
        await update.message.reply_text("Bạn đã tham gia!")
        return
    match.players.append(user.id)
    match.alive.append(user.id)
    await update.message.reply_text("✅ Đã tham gia!")

async def cmd_begin(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None):
    if chat_id is None:
        chat_id = update.effective_chat.id  # type: ignore
    match = matches.get(chat_id)
    if not match:
        await context.bot.send_message(chat_id, "❗ Chưa mở sảnh. /newgame trước nhé.")
        return
    if match.active:
        await context.bot.send_message(chat_id, "⚠️ Ván đang chạy.")
        return
    if not match.players:
        await context.bot.send_message(chat_id, "❗ Không có ai tham gia, hủy sảnh.")
        matches.pop(chat_id, None)
        return

    # vào game
    match.active = True
    random.shuffle(match.alive)
    pick_first_turn(match, context)
    await context.bot.send_message(chat_id, "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.")
    # người đầu
    first_user = await context.bot.get_chat_member(chat_id, curr_player_id(match))
    await send_rules(context, chat_id, first_user.user)
    await announce_turn(context, chat_id)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.pop(chat_id, None)
    if not m:
        await update.message.reply_text("Không có ván nào đang mở.")
        return
    await cancel_job_by_name(context, m.half_job_name)
    await cancel_job_by_name(context, m.timeout_job_name)
    await cancel_job_by_name(context, m.auto_begin_job_name)
    await update.message.reply_text("⛔ Đã dừng ván.")

# ====== XỬ LÝ VĂN BẢN ======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message
    chat_id = msg.chat_id
    user = msg.from_user

    match = matches.get(chat_id)
    if not match or not match.active:
        return

    # Không phải lượt người này -> bỏ qua
    if user.id != curr_player_id(match):
        return

    text = " ".join((msg.text or "").strip().split())
    match.spoke_in_turn = True   # đã nói gì đó trong lượt

    # Kiểm tra cụm 2 từ có nghĩa
    if not is_valid_phrase(text):
        # loại ngay theo yêu cầu
        await eliminate_current_player(context, chat_id, by_timeout=False)
        return

    # Không được trùng cụm đã dùng
    key = text.lower()
    if key in match.used:
        await eliminate_current_player(context, chat_id, by_timeout=False)
        return

    # chấp nhận
    match.used.add(key)
    match.current_phrase = text

    # chuyển lượt
    match.turn_idx = (match.turn_idx + 1) % len(match.alive)
    await msg.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    await announce_turn(context, chat_id)

# ====== KHỞI TẠO APP TELEGRAM + WEBHOOK ======
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN trong biến môi trường.")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

tg_app = build_app()

# FastAPI webhook (Render/Heroku dùng uvicorn chạy file này)
api = FastAPI()

@api.get("/")
async def root():
    return {"status": "ok"}

@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
