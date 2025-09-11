import os
import re
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

# ============ CẤU HÌNH ============
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))
HALFTIME_SECONDS = int(os.getenv("HALFTIME_SECONDS", str(ROUND_SECONDS // 2)))
AUTO_BEGIN_SECONDS = int(os.getenv("AUTO_BEGIN_SECONDS", "60"))
DICT_FILE = os.getenv("DICT_FILE", "dict_vi.txt").strip()
SLANG_FILE = os.getenv("SLANG_FILE", "slang_vi.txt").strip()

ALLOW_GENZ = os.getenv("ALLOW_GENZ", "1") == "1"   # bật/tắt cơ chế genZ linh hoạt
GENZ_FREQ = float(os.getenv("GENZ_FREQ", "2.2"))   # ngưỡng wordfreq (0-7)

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

SOLO_HINTS = [
    "Từ này có nghĩa thật không ? Anh nhắc cưng",
    "Cho bé cơ hội nữa ,",
    "Cơ hội cuối ! Nếu sai chuẩn bị xuống hàng ghế động vật ngồi !!!",
]

# ============ NẠP TỪ ĐIỂN ============
def _load_dict_file(fname: str) -> Set[str]:
    s: Set[str] = set()
    for p in [Path(fname), Path(__file__).parent / fname, Path("/opt/render/project/src") / fname]:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    t = " ".join(line.strip().lower().split())
                    if not t:
                        continue
                    parts = t.split()
                    if len(parts) == 2 and all(part.isalpha() for part in parts):
                        s.add(t)
            break
    return s

DICT: Set[str] = _load_dict_file(DICT_FILE)
SLANG: Set[str] = _load_dict_file(SLANG_FILE)
print(f"[DICT] Chuẩn: {len(DICT)} | SLANG: {len(SLANG)}")

# (Tuỳ chọn) wordfreq + symspell
try:
    from wordfreq import zipf_frequency
except Exception:
    zipf_frequency = None

try:
    from symspellpy import SymSpell, Verbosity
    _sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    _WORDS = set()
    for p in (DICT | SLANG):
        w1, w2 = p.split()
        _WORDS.add(w1); _WORDS.add(w2)
    for w in _WORDS:
        _sym.create_dictionary_entry(w, 1)
except Exception:
    _sym = None

# ============ KIỂM TRA ÂM TIẾT & NGHĨA ============
def norm2(text: str) -> str:
    return " ".join(text.strip().lower().split())

# Heuristic kiểm tra âm tiết hợp lệ (xấp xỉ)
_VALID_ONSET = r"(ngh|gh|ng|nh|ch|th|tr|ph|qu|gi|kh|quy|b|c|d|đ|g|h|k|l|m|n|p|q|r|s|t|v|x)?"
# Nucleus: đơn giản hoá; đủ dùng cho game
_VALID_NUCLEUS = r"(a|e|i|o|u|y|ai|ao|au|ay|eo|ia|iu|oa|oe|oi|ua|ui|uy|uoi|uya|uya|ye|ya|yo|yu|uu|uo|uou)"
_VALID_CODA = r"(c|ch|m|n|ng|nh|p|t)?"
_SYL_RE = re.compile(rf"^{_VALID_ONSET}{_VALID_NUCLEUS}{_VALID_CODA}$")

def _strip_diacritics(s: str) -> str:
    return unidecode(s.lower().strip())

def is_valid_syllable_vi(syllable: str) -> bool:
    s = _strip_diacritics(syllable)
    if not s.isalpha():
        return False
    return bool(_SYL_RE.match(s))

def is_two_word_form(text: str) -> Tuple[bool, List[str]]:
    t = norm2(text)
    parts = t.split()
    if len(parts) != 2:
        return False, parts
    if not all(p.isalpha() for p in parts):
        return False, parts
    if not all(is_valid_syllable_vi(p) for p in parts):
        return False, parts
    return True, parts

def _freq_ok(w: str) -> bool:
    if not zipf_frequency:
        return False
    return zipf_frequency(w, "vi") >= GENZ_FREQ

def is_meaningful(text: str) -> Tuple[bool, str, Dict]:
    """
    Trả về (ok, normalized_text, info)
      - Ưu tiên DICT -> SLANG
      - Nếu ALLOW_GENZ & có wordfreq: chấp nhận nếu cả 2 từ >= GENZ_FREQ
      - Nếu có symspell: autocorrect từng từ rồi thử lại
    """
    t = norm2(text)
    form_ok, parts = is_two_word_form(t)
    info = {"source": None, "w1": None, "w2": None, "note": None}
    if not form_ok:
        info["note"] = "form_invalid"
        return False, t, info

    w1, w2 = parts
    info["w1"], info["w2"] = w1, w2

    if t in DICT:
        info["source"] = "DICT"
        return True, t, info
    if ALLOW_GENZ and t in SLANG:
        info["source"] = "SLANG"
        return True, t, info

    if ALLOW_GENZ and _freq_ok(w1) and _freq_ok(w2):
        info["source"] = "FREQ"
        return True, t, info

    if _sym:
        sug1 = _sym.lookup(w1, Verbosity.CLOSEST, max_edit_distance=1)
        sug2 = _sym.lookup(w2, Verbosity.CLOSEST, max_edit_distance=1)
        c1 = sug1[0].term if sug1 else w1
        c2 = sug2[0].term if sug2 else w2
        cand = f"{c1} {c2}"
        if cand in DICT or (ALLOW_GENZ and cand in SLANG):
            info["source"] = "CORRECTED"
            return True, cand, info
        if ALLOW_GENZ and _freq_ok(c1) and _freq_ok(c2):
            info["source"] = "CORRECTED+FREQ"
            return True, cand, info

    info["note"] = "not_in_dict"
    return False, t, info

# ============ RHYME (ĐỐI VẦN) ============
ONSET_CLUSTERS = ["ngh","gh","ng","nh","ch","th","tr","ph","qu","gi","kh","quy"]
CONSONANTS = set(list("bcdfghjklmnpqrstvxđ"))

def rhyme_key(syllable: str) -> str:
    syl = unidecode(syllable.lower().strip())
    for cl in ONSET_CLUSTERS:
        if syl.startswith(cl):
            base = syl[len(cl):]
            return base or syl
    if syl and syl[0] in CONSONANTS:
        syl = syl[1:]
    return syl or syllable

def split_phrase(phrase: str) -> Tuple[str, str]:
    parts = norm2(phrase).split()
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]

def rhyme_match(prev_phrase: Optional[str], next_phrase: str) -> bool:
    if not prev_phrase:
        return True
    p1, p2 = split_phrase(prev_phrase)
    n1, _ = split_phrase(next_phrase)
    if not (p2 and n1):
        return False
    return rhyme_key(p2) == rhyme_key(n1)

# ============ GAME STATE ============
@dataclass
class Match:
    chat_id: int
    lobby_open: bool = False
    joined: List[int] = field(default_factory=list)
    active: bool = False
    turn_idx: int = 0
    current_player: Optional[int] = None
    current_phrase: Optional[str] = None

    auto_begin_task: Optional[asyncio.Task] = None
    half_task: Optional[asyncio.Task] = None
    timeout_task: Optional[asyncio.Task] = None

    used_phrases: Set[str] = field(default_factory=set)

    solo_mode: bool = False
    solo_warn_count: int = 0

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

# ============ TIỆN ÍCH ============
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
    if not match.current_phrase:
        candidates = list((DICT | SLANG) - match.used_phrases)
        return random.choice(candidates) if candidates else None
    _, prev_last = split_phrase(match.current_phrase)
    need_key = rhyme_key(prev_last)
    pool = (DICT | SLANG) - match.used_phrases
    candidates = [p for p in pool if rhyme_key(split_phrase(p)[0]) == need_key]
    return random.choice(candidates) if candidates else None

async def schedule_turn_timers(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
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

            if match.solo_mode:
                match.active = False
                match.cancel_turn_tasks()
                await context.bot.send_message(match.chat_id, "🏁 Ván solo kết thúc. Bot thắng 🤖")
                return

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

            match.turn_idx = (match.turn_idx + 1) % len(match.joined)
            pick_next_idx(match)
            who2 = await mention_user(context, match.chat_id, match.current_player)
            await context.bot.send_message(
                match.chat_id,
                f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** đúng vần với cụm trước.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await schedule_turn_timers(update, context, match)
        except asyncio.CancelledError:
            pass

    # chỉ đặt timer cho lượt người chơi
    if not match.solo_mode or (match.solo_mode and match.current_player is not None):
        loop = asyncio.get_running_loop()
        match.half_task = loop.create_task(half_warn())
        match.timeout_task = loop.create_task(timeout_kick())

# ============ HANDLERS ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào cả nhà! /newgame để mở sảnh, /join để tham gia.\n"
        f"Đủ 2 người, bot đếm ngược {AUTO_BEGIN_SECONDS}s rồi tự bắt đầu.\n"
        "Luật: đúng 2 từ, có nghĩa (DICT/SLANG hoặc linh hoạt), và **đối vần**: từ 1 của cụm mới cùng vần với từ 2 của cụm trước.\n"
        f"Từ điển: {len(DICT)} chuẩn + {len(SLANG)} slang."
    )

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DICT, SLANG
    DICT = _load_dict_file(DICT_FILE)
    SLANG = _load_dict_file(SLANG_FILE)
    await update.message.reply_text(f"🔁 Nạp lại: DICT={len(DICT)} | SLANG={len(SLANG)}")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Dùng: /check <cụm 2 từ>")
        return
    phrase = " ".join(context.args)
    form_ok, _ = is_two_word_form(phrase)
    ok, norm, info = is_meaningful(phrase)
    lines = []
    lines.append(f"🧪 `{phrase}` → `{norm}`")
    lines.append(f"• Âm tiết hợp lệ: {'✅' if form_ok else '❌'}")
    lines.append(f"• Có nghĩa: {'✅' if ok else '❌'}")
    src = info.get("source")
    if ok:
        lines.append(f"  ↳ Nguồn: {src or 'UNKNOWN'}")
    else:
        note = info.get("note")
        if note == "form_invalid":
            lines.append("  ↳ Lý do: ghép âm bất hợp pháp / không đúng 2 từ.")
        elif note == "not_in_dict":
            lines.append("  ↳ Không thấy trong DICT/SLANG và không qua ngưỡng tần suất.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = matches.get(chat_id) or Match(chat_id)
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

    # Vừa đủ 2 người → bắt đầu đếm ngược
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
        # SOLO
        m.solo_mode = True
        m.solo_warn_count = 0
        m.active = True
        m.turn_idx = 0
        m.current_player = m.joined[0]
        m.current_phrase = None
        await context.bot.send_message(
            m.chat_id,
            "🤖 Chỉ có 1 người tham gia → SOLO với bot.\n"
            "📘 Luật: đúng 2 từ • có nghĩa (tục/GenZ linh hoạt) • **đối vần** (từ 1 = vần từ 2 cụm trước)."
        )
        who = await mention_user(context, m.chat_id, m.current_player)
        await context.bot.send_message(
            m.chat_id, f"👉 {who} đi trước. Lượt đầu tự do.", parse_mode=ParseMode.MARKDOWN
        )
        await schedule_turn_timers(update, context, m)
        return

    # MULTI
    m.solo_mode = False
    m.active = True
    random.shuffle(m.joined)
    m.turn_idx = random.randrange(len(m.joined))
    m.current_player = m.joined[m.turn_idx]
    m.current_phrase = None
    await context.bot.send_message(
        m.chat_id,
        "🚀 Bắt đầu (multiplayer)! Sai luật hoặc hết giờ sẽ bị loại.\n"
        "📘 Luật: đúng 2 từ • có nghĩa (DICT/SLANG/linh hoạt) • **đối vần**."
    )
    who = await mention_user(context, m.chat_id, m.current_player)
    await context.bot.send_message(
        m.chat_id, f"👉 {who} đi trước. Lượt đầu tự do.", parse_mode=ParseMode.MARKDOWN
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

# ============ NHẬN CÂU TRẢ LỜI ============
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw = update.message.text
    m = matches.get(chat_id)
    if not m or not m.active:
        return
    if user_id != m.current_player:
        return

    # 1) Kiểu 2 từ + âm tiết hợp lệ
    form_ok, _ = is_two_word_form(raw)
    # 2) Nghĩa (lai)
    meaning_ok, normalized, info = is_meaningful(raw)
    # 3) Chưa dùng
    not_used = normalized not in m.used_phrases
    # 4) Đối vần
    rhyme_ok = rhyme_match(m.current_phrase, normalized)

    valid = form_ok and meaning_ok and not_used and rhyme_ok

    if not valid:
        if m.solo_mode:
            if m.solo_warn_count < 3:
                hint = SOLO_HINTS[m.solo_warn_count] if m.solo_warn_count < len(SOLO_HINTS) else SOLO_HINTS[-1]
                m.solo_warn_count += 1
                # Gợi ý ngắn lý do
                reasons = []
                if not form_ok: reasons.append("ghép âm/không đúng 2 từ")
                if not meaning_ok: reasons.append("không thấy nghĩa hợp lệ")
                if not not_used: reasons.append("đã dùng rồi")
                if not rhyme_ok: reasons.append("sai đối vần")
                extra = f" ({', '.join(reasons)})" if reasons else ""
                await update.message.reply_text(f"⚠️ {hint}{extra}")
                return
            else:
                await update.message.reply_text("❌ Sai liên tiếp. Bot thắng 🤖")
                m.active = False
                m.cancel_turn_tasks()
                return
        else:
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
    text = normalized
    m.used_phrases.add(text)
    m.current_phrase = text

    if m.solo_mode:
        m.solo_warn_count = 0
        await update.message.reply_text(f"✅ Hợp lệ ({info.get('source') or 'OK'}). Tới lượt bot 🤖")
        m.cancel_turn_tasks()

        bot_pick = random_bot_phrase(m)
        if not bot_pick or not rhyme_match(m.current_phrase, bot_pick):
            await context.bot.send_message(chat_id, "🤖 Hết chữ hợp vần… Bạn thắng! 🏆")
            m.active = False
            return

        m.used_phrases.add(bot_pick)
        m.current_phrase = bot_pick
        await context.bot.send_message(chat_id, f"🤖 Bot: **{bot_pick}**", parse_mode=ParseMode.MARKDOWN)

        await context.bot.send_message(chat_id, "👉 Tới lượt bạn. Gửi **cụm 2 từ** đúng vần.")
        await schedule_turn_timers(update, context, m)
        return

    await update.message.reply_text(f"✅ Hợp lệ ({info.get('source') or 'OK'}). Tới lượt kế tiếp!")
    m.turn_idx = (m.turn_idx + 1) % len(m.joined)
    m.current_player = m.joined[m.turn_idx]
    who2 = await mention_user(context, chat_id, m.current_player)
    await context.bot.send_message(
        chat_id, f"🟢 {who2} đến lượt. Gửi **cụm 2 từ** đúng vần với cụm trước.",
        parse_mode=ParseMode.MARKDOWN
    )
    await schedule_turn_timers(update, context, m)

# ============ APP ============
def build_app() -> Application:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
