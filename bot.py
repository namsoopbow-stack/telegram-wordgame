import os
import random
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ================== CẤU HÌNH / THÔNG ĐIỆP ==================
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", str(ROUND_SECONDS // 2)))
AUTO_BEGIN_SECONDS = int(os.getenv("AUTO_BEGIN_SECONDS", "60"))

HALF_WARNINGS = [
    "Còn 30 giây cuối để bạn suy nghĩ về cuộc đời:))",
    "Tắc ẻ đến vậy sao, 30 giây cuối nè :||",
    "30 vẫn chưa phải Tết, nhưng mi sắp hết giờ rồi. 30 giây!",
    "Mắc đitt rặn mãi không ra. 30 giây cuối ẻ!",
    "30 giây cuối ní ơi!"
]

WRONG_ANSWERS = [
    "IQ bạn cần phải xem xét lại, mời tiếp !!",
    "Mỗi thế cũng sai, GG cũng không cứu được !",
    "Sai rồi má, tra lại từ điển đi !",
    "Từ gì vậy má, học lại lớp 1 đi !!",
    "Ảo tiếng Việt hee",
    "Loại, người tiếp theo!",
    "Chưa tiến hoá hết à, từ này con người dùng sao… Sai bét!!"
]

TIMEOUT_MSG = "⏰ Hết giờ, mời bạn ra ngoài chờ !!"

# ================== NẠP TỪ ĐIỂN 2 TỪ ==================
def load_dict() -> Set[str]:
    """Nạp cụm 2 từ (mỗi dòng đúng 2 token chữ cái) từ file DICT_FILE."""
    fname = os.getenv("DICT_FILE", "dict_vi.txt").strip()
    search_paths = [
        Path(fname),
        Path(__file__).parent / fname,
        Path("/opt/render/project/src") / fname,  # Render
    ]
    used = None
    ok: Set[str] = set()
    dropped = 0
    for p in search_paths:
        if p.exists():
            used = p
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip().lower()
                    if not s:
                        continue
                    parts = s.split()
                    if len(parts) == 2 and all(part.isalpha() for part in parts):
                        ok.add(s)
                    else:
                        dropped += 1
            break
    if used is None:
        print(f"[DICT] ❌ Không tìm thấy file: {fname}")
    else:
        print(f"[DICT] ✅ {used} — hợp lệ: {len(ok)} | loại: {dropped}")
    return ok

DICT: Set[str] = load_dict()

def is_two_word_phrase_in_dict(s: str) -> bool:
    s = " ".join(s.strip().lower().split())
    parts = s.split()
    if len(parts) != 2:
        return False
    if not all(part.isalpha() for part in parts):
        return False
    return s in DICT

# ================== TRẠNG THÁI TRẬN ==================
@dataclass
class Match:
    chat_id: int
    lobby_open: bool = False
    joined: List[int] = field(default_factory=list)
    active: bool = False
    turn_idx: int = 0
    current_player: Optional[int] = None

    # tasks
    auto_begin_task: Optional[asyncio.Task] = None
    half_task: Optional[asyncio.Task] = None
    timeout_task: Optional[asyncio.Task] = None

    # ai đã dùng cụm này rồi (tránh lặp)
    used_phrases: Set[str] = field(default_factory=set)

    def cancel_turn_tasks(self):
        for t in (self.half_task, self.timeout_task):
            if t and not t.done():
                t.cancel()
        self.half_task = None
        self.timeout_task = None

    def cancel_auto_begin(self):
        if self.auto_begin_task and not self.auto_begin_task.done():
            self.auto_begin_task.cancel()
        self.auto_begin_task = None

matches: Dict[int, Match] = {}

# ================== TIỆN ÍCH ==================
async def mention_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> str:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        name = member.user.first_name or ""
        return f"[{name}](tg://user?id={user_id})"
    except Exception:
        return f"user_{user_id}"

def pick_next_idx(match: Match):
    if not match.joined:
        return
    match.turn_idx %= len(match.joined)
    match.current_player = match.joined[match.turn_idx]

async def schedule_turn_timers(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    match.cancel_turn_tasks()

    async def half_warn():
        try:
            await asyncio.sleep(HALFTIME_SECONDS)
            # Chỉ nhắc nếu vẫn là người này & chưa gửi gì
            if match.active:
                who = await mention_user(context, match.chat_id, match.current_player)
                msg = random.choice(HALF_WARNINGS)
                await context.bot.send_message(
                    match.chat_id, f"⏳ {who} — {msg}", parse_mode=ParseMode.MARKDOWN
                )
        except asyncio.CancelledError:
            pass

    async def timeout_kick():
        try:
            await asyncio.sleep(ROUND_SECONDS)
            if not match.active:
                return
            who = match.current_player
            who_m = await mention_user(context, match.chat_id, who)
            await context.bot.send_message(match.chat_id, f"❌ {who_m} — {TIMEOUT_MSG}", parse_mode=ParseMode.MARKDOWN)

            # loại người chơi quá giờ
            if who in match.joined:
                idx = match.joined.index(who)
                match.joined.pop(idx)
                if idx <= match.turn_idx and match.turn_idx > 0:
                    match.turn_idx -= 1

            if len(match.joined) <= 1:
                # kết thúc
                if match.joined:
                    winner = await mention_user(context, match.chat_id, match.joined[0])
                    await context.bot.send_message(match.chat_id, f"🏆 {winner} thắng cuộc!", parse_mode=ParseMode.MARKDOWN)
                match.active = False
                match.cancel_turn_tasks()
                return

            # chuyển lượt
            match.turn_idx = (match.turn_idx + 1) % len(match.joined)
            pick_next_idx(match)
            who2 = await mention_user(context, match.chat_id, match.current_player)
            await context.bot.send_message(
                match.chat_id,
                f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** có nghĩa (có trong từ điển).",
                parse_mode=ParseMode.MARKDOWN,
            )
            await schedule_turn_timers(update, context, match)
        except asyncio.CancelledError:
            pass

    loop = asyncio.get_running_loop()
    match.half_task = loop.create_task(half_warn())
    match.timeout_task = loop.create_task(timeout_kick())

# ================== HANDLERS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia.\n"
        f"Nếu không ai /begin, bot sẽ tự bắt đầu sau {AUTO_BEGIN_SECONDS}s.\n"
        f"Từ điển hiện có: {len(DICT)} cụm 2 từ."
    )

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DICT
    DICT = load_dict()
    await update.message.reply_text(f"🔁 Đã nạp lại từ điển: {len(DICT)} cụm 2 từ.")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id) or Match(chat_id)
    # reset
    m.lobby_open = True
    m.joined = []
    m.active = False
    m.turn_idx = 0
    m.current_player = None
    m.used_phrases.clear()
    m.cancel_turn_tasks()
    m.cancel_auto_begin()
    matches[chat_id] = m

    async def auto_begin():
        try:
            await asyncio.sleep(AUTO_BEGIN_SECONDS)
            if m.lobby_open and not m.active:
                await force_begin(update, context, m)
        except asyncio.CancelledError:
            pass

    loop = asyncio.get_running_loop()
    m.auto_begin_task = loop.create_task(auto_begin())

    await update.message.reply_text(
        f"🎮 Sảnh mở! /join để tham gia. Không ai /begin thì {AUTO_BEGIN_SECONDS}s nữa tự bắt đầu."
    )

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    m = matches.get(chat_id)
    if not m or not m.lobby_open:
        await update.message.reply_text("Chưa /newgame mà nhập lố nè 😛")
        return
    if user_id in m.joined:
        await update.message.reply_text("Bạn đã tham gia!")
        return
    m.joined.append(user_id)
    who = await mention_user(context, chat_id, user_id)
    await update.message.reply_text(f"➕ {who} đã tham gia!", parse_mode=ParseMode.MARKDOWN)

async def force_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, m: Match):
    if m.active:
        return
    m.lobby_open = False
    if len(m.joined) == 0:
        await context.bot.send_message(m.chat_id, "Không có ai tham gia nên huỷ ván.")
        return
    if len(m.joined) == 1:
        await context.bot.send_message(m.chat_id, "Chỉ có 1 người chơi. Cần ≥2 người để chơi.")
        return

    random.shuffle(m.joined)
    m.active = True
    m.turn_idx = random.randrange(len(m.joined))
    pick_next_idx(m)
    m.cancel_auto_begin()

    await context.bot.send_message(
        m.chat_id,
        "🚀 Bắt đầu! Sai luật hoặc hết giờ sẽ bị loại.\n"
        "📘 Luật: đúng 2 từ • mỗi từ ≥2 ký tự • mỗi từ phải có nghĩa (nằm trong từ điển).",
    )
    who = await mention_user(context, m.chat_id, m.current_player)
    await context.bot.send_message(
        m.chat_id,
        f"👉 {who} đi trước. Gửi **cụm 2 từ** có nghĩa bất kỳ.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await schedule_turn_timers(update, context, m)

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa /newgame kìa.")
        return
    await force_begin(update, context, m)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = " ".join(update.message.text.strip().lower().split())

    m = matches.get(chat_id)
    if not m or not m.active:
        return  # bỏ qua tin nhắn khi không chơi

    # chỉ xét tin của người đang tới lượt
    if user_id != m.current_player:
        return

    # kiểm tra hợp lệ trong từ điển
    if not is_two_word_phrase_in_dict(text) or text in m.used_phrases:
        msg = random.choice(WRONG_ANSWERS)
        await update.message.reply_text(f"❌ {msg}")
        # loại người chơi
        idx = m.joined.index(user_id)
        m.joined.pop(idx)
        if idx <= m.turn_idx and m.turn_idx > 0:
            m.turn_idx -= 1

        if len(m.joined) <= 1:
            if m.joined:
                winner = await mention_user(context, chat_id, m.joined[0])
                await context.bot.send_message(chat_id, f"🏆 {winner} thắng cuộc!", parse_mode=ParseMode.MARKDOWN)
            m.active = False
            m.cancel_turn_tasks()
            return

        # chuyển lượt
        m.turn_idx = (m.turn_idx + 1) % len(m.joined)
        pick_next_idx(m)
        who2 = await mention_user(context, chat_id, m.current_player)
        await context.bot.send_message(
            chat_id, f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** có nghĩa.", parse_mode=ParseMode.MARKDOWN
        )
        await schedule_turn_timers(update, context, m)
        return

    # hợp lệ
    m.used_phrases.add(text)
    await update.message.reply_text("✅ Hợp lệ. Tiếp tục!")

    # chuyển lượt
    m.turn_idx = (m.turn_idx + 1) % len(m.joined)
    pick_next_idx(m)
    who2 = await mention_user(context, chat_id, m.current_player)
    await context.bot.send_message(
        chat_id, f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** có nghĩa.", parse_mode=ParseMode.MARKDOWN
    )
    await schedule_turn_timers(update, context, m)

# ================== TẠO APP ==================
def build_app() -> Application:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
