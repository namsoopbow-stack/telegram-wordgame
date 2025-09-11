import os
import random
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from unidecode import unidecode
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ================== CẤU HÌNH / THÔNG ĐIỆP ==================
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", str(ROUND_SECONDS // 2)))
AUTO_BEGIN_SECONDS = int(os.getenv("AUTO_BEGIN_SECONDS", "60"))
DICT_FILE = os.getenv("DICT_FILE", "dict_vi.txt").strip()

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

# 3 câu “nhắc” dành cho CHẾ ĐỘ 1 NGƯỜI (sai nhưng chưa loại ngay)
SOLO_HINTS = [
    "Từ này có nghĩa thật không ? Anh nhắc cưng",
    "Cho bé cơ hội nữa ,",
    "Cơ hội cuối ! Nếu sai chuẩn bị xuống hàng ghế động vật ngồi !!!",
]

# ================== NẠP TỪ ĐIỂN 2 TỪ ==================
def load_dict(path_hint: str = DICT_FILE) -> Set[str]:
    """Nạp cụm 2 từ (mỗi dòng đúng 2 token chữ cái) từ file DICT_FILE."""
    search_paths = [
        Path(path_hint),
        Path(__file__).parent / path_hint,
        Path("/opt/render/project/src") / path_hint,  # Render
    ]
    used = None
    ok: Set[str] = set()
    dropped = 0
    for p in search_paths:
        if p.exists():
            used = p
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    s = " ".join(line.strip().lower().split())
                    if not s:
                        continue
                    parts = s.split()
                    # giữ đúng 2 token, đều là chữ (cho “2 từ/2 vần” chuẩn)
                    if len(parts) == 2 and all(part.isalpha() for part in parts):
                        ok.add(s)
                    else:
                        dropped += 1
            break
    if used is None:
        print(f"[DICT] ❌ Không tìm thấy file: {path_hint}")
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

# ================== RHYME (VẦN) ==================
# Lấy "vần" tiếng Việt xấp xỉ: bỏ dấu, bỏ phụ âm đầu; giữ nguyên âm + phụ âm cuối.
# Ví dụ: "cá" -> "a"; "heo" -> "eo"; "trăng" -> "ang"; "quốc" -> "oc"/"uoc" (xấp xỉ).
# Lưu ý: đây là heuristic đủ dùng để chơi; không phải bộ tách âm vị hoàn hảo.
ONSET_CLUSTERS = [
    "ngh","gh","ng","nh","ch","th","tr","ph","qu","gi","kh","th","qu","qu","quy"
]
CONSONANTS = set(list("bcdfghjklmnpqrstvxđ"))

def rhyme_key(syllable: str) -> str:
    # chuẩn hóa: lower, bỏ dấu thanh (unidecode), gom space
    syl = unidecode(syllable.lower().strip())
    # đặc biệt: 'qu' và 'gi' thường coi như phụ âm đầu
    for cl in ONSET_CLUSTERS:
        if syl.startswith(cl):
            return syl[len(cl):] or syl  # nếu rỗng, trả về chính nó
    # nếu bắt đầu bằng phụ âm đơn -> bỏ 1 ký tự
    if syl and syl[0] in CONSONANTS:
        syl = syl[1:]
    return syl or syllable  # fallback

def split_phrase(phrase: str) -> Tuple[str, str]:
    parts = " ".join(phrase.strip().lower().split()).split()
    if len(parts) != 2:
        return ("","")
    return parts[0], parts[1]

def rhyme_match(prev_phrase: Optional[str], next_phrase: str) -> bool:
    """Lượt sau phải bắt đầu bằng từ 1 có 'vần' = vần của từ 2 ở cụm trước."""
    if not prev_phrase:
        return True  # lượt đầu tự do
    p1, p2 = split_phrase(prev_phrase)
    n1, n2 = split_phrase(next_phrase)
    if not (p2 and n1):
        return False
    return rhyme_key(p2) == rhyme_key(n1)

# ================== TRẠNG THÁI TRẬN ==================
@dataclass
class Match:
    chat_id: int
    lobby_open: bool = False
    joined: List[int] = field(default_factory=list)
    active: bool = False
    turn_idx: int = 0
    current_player: Optional[int] = None
    current_phrase: Optional[str] = None  # cụm hợp lệ trước đó

    # tasks
    auto_begin_task: Optional[asyncio.Task] = None
    half_task: Optional[asyncio.Task] = None
    timeout_task: Optional[asyncio.Task] = None

    used_phrases: Set[str] = field(default_factory=set)

    # Chế độ 1 người
    solo_mode: bool = False
    solo_warn_count: int = 0  # số lần đã “nhắc” ở lượt hiện tại

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

def random_bot_phrase(match: Match) -> Optional[str]:
    """Bot chọn 1 cụm chưa dùng thỏa vần."""
    # Người chơi vừa nói -> lấy vần từ 2:
    if not match.current_phrase:
        # lượt đầu solo: bot nhả ngẫu nhiên
        candidates = list(DICT - match.used_phrases)
        return random.choice(candidates) if candidates else None
    _, prev_last = split_phrase(match.current_phrase)
    need_key = rhyme_key(prev_last)
    # cần cụm có từ 1 trùng vần
    candidates = [p for p in DICT - match.used_phrases if rhyme_key(split_phrase(p)[0]) == need_key]
    return random.choice(candidates) if candidates else None

async def schedule_turn_timers(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    """Đặt nhắc 30s và loại sau 60s cho người đang tới lượt (chỉ áp cho người chơi, không áp cho bot)."""
    match.cancel_turn_tasks()

    async def half_warn():
        try:
            await asyncio.sleep(HALFTIME_SECONDS)
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

            # SOLO: hết giờ -> thua, kết thúc
            if match.solo_mode:
                match.active = False
                match.cancel_turn_tasks()
                await context.bot.send_message(match.chat_id, "🏁 Ván solo kết thúc. Bot thắng 🤖")
                return

            # MULTI: loại player, chuyển lượt/trao cúp nếu còn 1
            if who in match.joined:
                idx = match.joined.index(who)
                match.joined.pop(idx)
                if idx <= match.turn_idx and match.turn_idx > 0:
                    match.turn_idx -= 1

            if len(match.joined) <= 1:
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
                f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** có nghĩa (đúng vần với cụm trước).",
                parse_mode=ParseMode.MARKDOWN,
            )
            await schedule_turn_timers(update, context, match)
        except asyncio.CancelledError:
            pass

    # chỉ đặt timer cho lượt người chơi (không đặt khi tới lượt “bot ảo”)
    if not match.solo_mode or (match.solo_mode and match.current_player is not None):
        loop = asyncio.get_running_loop()
        match.half_task = loop.create_task(half_warn())
        match.timeout_task = loop.create_task(timeout_kick())

# ================== HANDLERS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia.\n"
        f"Đủ 2 người, bot đếm ngược {AUTO_BEGIN_SECONDS}s rồi tự bắt đầu.\n"
        f"Luật: đúng 2 từ, có trong từ điển, và **đối vần** (từ 1 của cụm mới phải cùng vần với từ 2 của cụm trước).\n"
        f"Từ điển hiện có: {len(DICT)} cụm 2 từ."
    )

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DICT
    DICT = load_dict(DICT_FILE)
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
    m.current_phrase = None
    m.used_phrases.clear()
    m.solo_mode = False
    m.solo_warn_count = 0
    m.cancel_turn_tasks()
    m.cancel_auto_begin()
    matches[chat_id] = m

    await update.message.reply_text(
        f"🎮 Sảnh mở! /join để tham gia.\n"
        f"➡️ Khi **đủ 2 người**, bot sẽ đếm ngược {AUTO_BEGIN_SECONDS}s rồi tự bắt đầu."
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

    # Khi vừa đủ 2 người → bắt đầu đếm ngược 60s (dù sau đó có thêm người vẫn START đúng lịch)
    if len(m.joined) == 2 and m.auto_begin_task is None:
        async def auto_begin():
            try:
                await asyncio.sleep(AUTO_BEGIN_SECONDS)
                if m.lobby_open and not m.active and len(m.joined) >= 2:
                    await force_begin(update, context, m)
            except asyncio.CancelledError:
                pass
        loop = asyncio.get_running_loop()
        m.auto_begin_task = loop.create_task(auto_begin())
        await context.bot.send_message(chat_id, f"⏳ Đủ 2 người rồi. {AUTO_BEGIN_SECONDS}s nữa bắt đầu tự động!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id)
    if not m:
        await update.message.reply_text("Chưa /newgame kìa.")
        return
    await force_begin(update, context, m)

async def force_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, m: Match):
    if m.active:
        return
    m.lobby_open = False
    m.cancel_auto_begin()

    if len(m.joined) == 0:
        await context.bot.send_message(m.chat_id, "Không có ai tham gia nên huỷ ván.")
        return

    if len(m.joined) == 1:
        # ===== SOLO MODE =====
        m.solo_mode = True
        m.solo_warn_count = 0
        m.active = True
        m.turn_idx = 0
        m.current_player = m.joined[0]
        m.current_phrase = None
        await context.bot.send_message(
            m.chat_id,
            "🤖 Chỉ có 1 người tham gia → chơi SOLO với bot.\n"
            "📘 Luật: đúng 2 từ • có trong từ điển • **đối vần** (từ 1 của cụm mới phải cùng vần với từ 2 của cụm trước).\n"
            "Sai sẽ được nhắc tối đa 3 lần.",
        )
        who = await mention_user(context, m.chat_id, m.current_player)
        await context.bot.send_message(
            m.chat_id,
            f"👉 {who} đi trước. Gửi **cụm 2 từ** bất kỳ (lượt đầu tự do).",
            parse_mode=ParseMode.MARKDOWN,
        )
        await schedule_turn_timers(update, context, m)
        return

    # ===== MULTIPLAYER =====
    m.solo_mode = False
    m.active = True
    random.shuffle(m.joined)
    m.turn_idx = random.randrange(len(m.joined))
    m.current_player = m.joined[m.turn_idx]
    m.current_phrase = None

    await context.bot.send_message(
        m.chat_id,
        "🚀 Bắt đầu (multiplayer)! Sai luật hoặc hết giờ sẽ bị loại.\n"
        "📘 Luật: đúng 2 từ • có trong từ điển • **đối vần** (từ 1 của cụm mới phải cùng vần với từ 2 của cụm trước).",
    )
    who = await mention_user(context, m.chat_id, m.current_player)
    await context.bot.send_message(
        m.chat_id,
        f"👉 {who} đi trước. Gửi **cụm 2 từ** bất kỳ (lượt đầu tự do).",
        parse_mode=ParseMode.MARKDOWN,
    )
    await schedule_turn_timers(update, context, m)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id)
    if not m:
        await update.message.reply_text("Không có ván nào.")
        return
    m.lobby_open = False
    m.active = False
    m.cancel_turn_tasks()
    m.cancel_auto_begin()
    await update.message.reply_text("⛔ Đã dừng ván hiện tại.")

# ================== NHẬN CÂU TRẢ LỜI ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = " ".join(update.message.text.strip().lower().split())

    m = matches.get(chat_id)
    if not m or not m.active:
        return  # bỏ qua khi không chơi

    # chỉ xét tin của người đang tới lượt (multiplayer) hoặc của người chơi (solo)
    if user_id != m.current_player:
        return

    # 1) đúng 2 từ, có trong từ điển, chưa dùng
    basic_ok = is_two_word_phrase_in_dict(text) and (text not in m.used_phrases)
    # 2) đúng luật vần (trừ lượt đầu)
    rhyme_ok = rhyme_match(m.current_phrase, text)

    if not (basic_ok and rhyme_ok):
        if m.solo_mode:
            # SOLO: nhắc tối đa 3 lần, không loại ngay
            if m.solo_warn_count < 3:
                hint = SOLO_HINTS[m.solo_warn_count] if m.solo_warn_count < len(SOLO_HINTS) else SOLO_HINTS[-1]
                m.solo_warn_count += 1
                await update.message.reply_text(f"⚠️ {hint}")
                return
            else:
                # quá 3 nhắc -> thua
                await update.message.reply_text("❌ Sai liên tiếp. Bot thắng 🤖")
                m.active = False
                m.cancel_turn_tasks()
                return
        else:
            # MULTI: loại ngay
            msg = random.choice(WRONG_ANSWERS)
            await update.message.reply_text(f"❌ {msg}")
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
            m.current_player = m.joined[m.turn_idx]
            who2 = await mention_user(context, chat_id, m.current_player)
            await context.bot.send_message(
                chat_id, f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** đúng vần với cụm trước.",
                parse_mode=ParseMode.MARKDOWN
            )
            await schedule_turn_timers(update, context, m)
            return

    # ===== HỢP LỆ =====
    m.used_phrases.add(text)
    m.current_phrase = text

    if m.solo_mode:
        # Reset bộ đếm cảnh báo cho lượt kế
        m.solo_warn_count = 0
        await update.message.reply_text("✅ Hợp lệ. Tới lượt bot 🤖")
        # Huỷ timer vì bot trả ngay
        m.cancel_turn_tasks()

        bot_pick = random_bot_phrase(m)
        if not bot_pick:
            await context.bot.send_message(chat_id, "🤖 Bot hết chữ rồi… Bạn thắng! 🏆")
            m.active = False
            return

        # Kiểm tra bot có tuân luật vần không (phải đúng theo cụm của bạn vừa nói)
        if not rhyme_match(m.current_phrase, bot_pick):
            # nếu hiếm khi không tìm được cụm hợp vần: bot chịu thua
            await context.bot.send_message(chat_id, "🤖 Hết chữ hợp vần… Bạn thắng! 🏆")
            m.active = False
            return

        m.used_phrases.add(bot_pick)
        m.current_phrase = bot_pick
        await context.bot.send_message(chat_id, f"🤖 Bot: **{bot_pick}**", parse_mode=ParseMode.MARKDOWN)

        # Trả lượt lại cho người chơi + đặt lại đồng hồ
        await context.bot.send_message(chat_id, "👉 Tới lượt bạn. Gửi **cụm 2 từ** đúng vần.")
        await schedule_turn_timers(update, context, m)
        return

    # MULTIPLAYER: chuyển lượt bình thường
    await update.message.reply_text("✅ Hợp lệ. Tới lượt kế tiếp!")
    m.turn_idx = (m.turn_idx + 1) % len(m.joined)
    m.current_player = m.joined[m.turn_idx]
    who2 = await mention_user(context, chat_id, m.current_player)
    await context.bot.send_message(
        chat_id, f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** đúng vần với cụm trước.",
        parse_mode=ParseMode.MARKDOWN
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
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
