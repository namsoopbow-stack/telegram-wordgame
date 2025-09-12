# bot.py — Đối chữ "cụm 2 từ có nghĩa" + /iu tỏ tình (PTB 21.x, webhook)
# - /newgame hoặc /batdau mở sảnh, đếm ngược AUTO_LOBBY giây
# - 0 join: hủy, 1 join: chơi với bot, >=2: bot làm trọng tài
# - Mỗi lượt ROUND_SECONDS (nhắc ở HALF_TIME)
# - Cụm sau phải bắt đầu bằng từ thứ 2 của cụm trước (khớp không dấu)
# - Nghĩa: có trong dict_vi.txt / slang_vi.txt (có/không dấu) hoặc zipf>=GENZ_ZIPF (wordfreq)
# - /iu: chỉ @yhck2 gọi; @xiaoc6789 bấm nút nào cũng “Em đồng ý !! Yêu Anh 🥰”, người khác “Thiệu ơi !! Yêu Anh Nam Đii”
# - Tương thích webhook.py: build_app() trả một wrapper có initialize/start/stop/shutdown/process_update

import os, re, json, random, asyncio
from typing import Dict, List, Set, Optional, Tuple
from datetime import datetime

from unidecode import unidecode

try:
    from wordfreq import zipf_frequency
except Exception:
    zipf_frequency = None

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN")

ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))
HALF_TIME     = int(os.getenv("HALF_TIME", "30"))
AUTO_LOBBY    = int(os.getenv("AUTO_LOBBY", "60"))

DICT_FILE  = os.getenv("DICT_VI",  "dict_vi.txt")
SLANG_FILE = os.getenv("SLANG_VI", "slang_vi.txt")
GENZ_ZIPF  = float(os.getenv("GENZ_ZIPF", "2.2"))

SPECIAL_CALLER   = os.getenv("IU_CALLER", "@yhck2").lower()
SPECIAL_ACCEPTOR = os.getenv("IU_ACCEPTOR", "@xiaoc6789").lower()

# =========================
# HELPERS (normalize + dict)
# =========================
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"\s+", " ", s)

def _norm_nodiac(s: str) -> str:
    return _norm(unidecode(s))

def load_list(path: str) -> Set[str]:
    bag: Set[str] = set()
    if not os.path.exists(path):
        return bag
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            t = _norm(line)
            if not t: 
                continue
            bag.add(t)                 # có dấu
            bag.add(_norm_nodiac(t))   # thêm bản không dấu
    return bag

DICT_SET  = load_list(DICT_FILE)
SLANG_SET = load_list(SLANG_FILE)

def is_meaningful_token(tok: str) -> bool:
    if not tok or len(tok) < 2:
        return False
    t1 = _norm(tok)
    t0 = _norm_nodiac(tok)
    if t1 in DICT_SET or t0 in DICT_SET:
        return True
    if t1 in SLANG_SET or t0 in SLANG_SET:
        return True
    if zipf_frequency:
        try:
            if zipf_frequency(t1, "vi") >= GENZ_ZIPF:
                return True
            if t0 != t1 and zipf_frequency(t0, "vi") >= GENZ_ZIPF:
                return True
        except Exception:
            pass
    return False

def is_valid_two_word_phrase(text: str) -> Tuple[bool, str, List[str]]:
    t = _norm(text)
    toks = t.split()
    if len(toks) != 2:
        return False, "❌ Phải gửi **cụm 2 từ**.", []
    bad = [w for w in toks if not is_meaningful_token(w)]
    if bad:
        return False, f"❌ Từ **{bad[0]}** nghe không có nghĩa (hoặc hiếm quá).", []
    return True, "", toks

def link_rule_ok(prev_tokens: List[str], new_tokens: List[str]) -> Tuple[bool, str]:
    if not prev_tokens:
        return True, ""
    need = _norm_nodiac(prev_tokens[1])
    got  = _norm_nodiac(new_tokens[0])
    if need != got:
        return False, f"❌ Sai luật nối chữ: phải bắt đầu bằng **{prev_tokens[1]} …**"
    return True, ""

def pick_meaningful_word(exclude_first: str, used: Set[str]) -> Optional[str]:
    pool = [w for w in DICT_SET if " " not in w and len(w) >= 2]
    pool += [w for w in SLANG_SET if " " not in w and len(w) >= 2]
    random.shuffle(pool)
    for cand in pool:
        if _norm_nodiac(cand) == _norm_nodiac(exclude_first):
            continue
        phrase = f"{exclude_first} {cand}"
        if _norm(phrase) not in used:
            return cand
    if zipf_frequency:
        commons = ["đẹp","lên","xuống","mạnh","nhanh","vội","đã","nữa","liền","ngay"]
        random.shuffle(commons)
        for cand in commons:
            if _norm(f"{exclude_first} {cand}") not in used:
                return cand
    return None

# =========================
# MESSAGES
# =========================
LOBBY_TEXT = (
    "Chào nhóm!\n"
    "Gõ /join để tham gia. Sau {sec}s nếu:\n"
    "• 0 người: ❌ Hủy ván\n"
    "• 1 người: 🤖 Bạn chơi với bot\n"
    "• 2+ người: 👑 Bot làm trọng tài\n\n"
    "📘 Luật:\n"
    "• Gửi **cụm 2 từ**\n"
    "• Mỗi từ phải **có nghĩa** (từ điển/slang hoặc phổ dụng)\n"
    "• Cụm sau **bắt đầu bằng đuôi** của cụm trước (VD: “con heo” → “**heo** nái”)\n"
    "• Sai luật/hết giờ → loại."
)

REMINDERS = [
    "Nhanh nhanh lên bạn ơi, thời gian không chờ ai đâu!",
    "Có đoán được không? Chậm thế!",
    "IQ chỉ đến thế thôi sao? Nhanh cái não lên!",
    "Suy nghĩ gì nữa!!! Đoán đêeee!",
    "Vẫn chưa có kết quả sao?? Não 🐷 à!!!",
    "Tỉnh táo lên nào, cơ hội đang trôi kìa!",
    "Bình tĩnh nhưng đừng *từ tốn* quá bạn ơi!",
    "Đếm ngược rồi đó, làm phát chất lượng đi!",
    "Đố mẹo chứ đâu phải đố đời đâu 🤭",
    "Thời gian là vàng, còn bạn là... bạc phếch!",
    "Gợi ý nằm trong bốn chữ: **cụm hai từ**!",
]

OK_CHEERS = [
    "✅ Thôi được, công nhận bạn không gà lắm!",
    "✅ Quá ghê, xin nhận một cú cúi đầu!",
    "✅ Chuẩn bài, khỏi bàn!",
    "✅ Đỉnh của chóp!",
]

TIME_WARNINGS = [
    "⏰ Cú lừa à? Không, chỉ còn **ít thời gian** thôi!",
    "⏰ Nhanh lên, nửa thời gian đã trôi!",
]

WRONG_FMT = [
    "❌ Không đúng. Còn {left} lần 'trật lất' nữa!",
    "❌ Sai rồi. Còn {left} cơ hội!",
    "❌ No no. Còn {left} lần!",
    "❌ Trượt. {left} lần còn lại!",
]

# =========================
# GAME STATE
# =========================
class Game:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.lobby_open = False
        self.players: List[int] = []
        self.single_vs_bot = False

        self.active = False
        self.prev_tokens: List[str] = []
        self.used: Set[str] = set()
        self.mistakes_left: int = 3
        self.job_ids: List[str] = []

    def clear_jobs(self, context: ContextTypes.DEFAULT_TYPE):
        for jid in list(self.job_ids):
            for j in context.job_queue.get_jobs_by_name(jid):
                j.schedule_removal()
            self.job_ids.clear()

GAMES: Dict[int, Game] = {}

def get_game(chat_id: int) -> Game:
    if chat_id not in GAMES:
        GAMES[chat_id] = Game(chat_id)
    return GAMES[chat_id]

# =========================
# APP FACTORY
# =========================
def make_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start"], cmd_start))
    app.add_handler(CommandHandler(["newgame","batdau"], cmd_newgame))
    app.add_handler(CommandHandler(["join"], cmd_join))
    app.add_handler(CommandHandler(["ketthuc"], cmd_stop))
    app.add_handler(CommandHandler(["iu"], cmd_iu))
    app.add_handler(CallbackQueryHandler(cb_iu_buttons, pattern=r"^iu:(yes|no)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Chào nhóm! /newgame hoặc /batdau để mở sảnh, /join để tham gia, /ketthuc để dừng.\n"
        f"⏱️ Mỗi lượt {ROUND_SECONDS}s (nhắc ở {HALF_TIME}s).\n"
        f"Từ điển: ~{len(DICT_SET)//2} mục, slang: ~{len(SLANG_SET)//2} mục."
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    g.lobby_open = True
    g.players = []
    g.active = False
    g.single_vs_bot = False
    g.prev_tokens = []
    g.used = set()
    g.mistakes_left = 3
    g.clear_jobs(context)

    await update.effective_message.reply_text(LOBBY_TEXT.format(sec=AUTO_LOBBY), parse_mode=ParseMode.MARKDOWN)

    jid = f"lobby:{chat_id}:{datetime.now().timestamp()}"
    g.job_ids.append(jid)
    context.job_queue.run_once(close_lobby, AUTO_LOBBY, chat_id=chat_id, name=jid)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    u = update.effective_user
    if not g.lobby_open:
        await update.effective_message.reply_text("Không có sảnh mở. Dùng /newgame trước.")
        return
    if u.id in g.players:
        await update.effective_message.reply_text("Bạn đã /join rồi!")
        return
    g.players.append(u.id)
    await update.effective_message.reply_html(f"✅ {u.mention_html()} đã tham gia!")

async def close_lobby(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    g = get_game(chat_id)
    g.lobby_open = False
    n = len(g.players)
    if n == 0:
        await context.bot.send_message(chat_id, "⛔ Không ai tham gia. Hủy ván.")
        return
    if n == 1:
        g.single_vs_bot = True
        await context.bot.send_message(chat_id, "🤖 Chỉ có 1 người. Bạn sẽ chơi với bot!")
    else:
        g.single_vs_bot = False
        await context.bot.send_message(chat_id, f"👥 Có {n} người. Bắt đầu thôi!")

    await start_round(chat_id, context)

async def start_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    g.active = True
    g.prev_tokens = []
    g.used = set()
    g.mistakes_left = 3
    g.clear_jobs(context)

    await context.bot.send_message(
        chat_id,
        "🚀 Bắt đầu! Gửi **cụm 2 từ có nghĩa**. Sai luật/hết giờ → loại.",
        parse_mode=ParseMode.MARKDOWN,
    )
    schedule_timers(chat_id, context)

def schedule_timers(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    g.clear_jobs(context)

    jid1 = f"half:{chat_id}:{datetime.now().timestamp()}"
    g.job_ids.append(jid1)
    context.job_queue.run_once(half_warn, HALF_TIME, chat_id=chat_id, name=jid1)

    jid2 = f"end:{chat_id}:{datetime.now().timestamp()}"
    g.job_ids.append(jid2)
    context.job_queue.run_once(timeup, ROUND_SECONDS, chat_id=chat_id, name=jid2)

async def half_warn(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id, random.choice(TIME_WARNINGS))
    await context.bot.send_message(chat_id, "💡 " + random.choice(REMINDERS))

async def timeup(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    g = get_game(chat_id)
    if not g.active:
        return
    g.active = False
    await context.bot.send_message(chat_id, "⏳ Hết giờ! Ván dừng ở đây.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    g.active = False
    g.lobby_open = False
    g.clear_jobs(context)
    await update.effective_message.reply_text("🛑 Đã kết thúc ván.")

# =========================
# /iu — tỏ tình
# =========================
async def cmd_iu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    username = ("@" + (user.username or "")).lower()
    if username != SPECIAL_CALLER:
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Đồng ý 💚", callback_data="iu:yes"),
          InlineKeyboardButton("Không 💔", callback_data="iu:no")]]
    )
    await context.bot.send_message(chat_id, "Yêu Em Thiệu 🥰 Làm Người Yêu Anh Nhé !!!", reply_markup=kb)

async def cb_iu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    chat_id = q.message.chat_id
    user = q.from_user
    username = ("@" + (user.username or "")).lower()
    text = "Em đồng ý !! Yêu Anh 🥰" if username == SPECIAL_ACCEPTOR else "Thiệu ơi !! Yêu Anh Nam Đii"
    await q.message.reply_text(text)

# =========================
# NHẬN CÂU TRẢ LỜI
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g.active:
        return

    msg: Message = update.effective_message
    text = msg.text or ""

    ok, err, toks = is_valid_two_word_phrase(text)
    if not ok:
        await msg.reply_text(err, parse_mode=ParseMode.MARKDOWN)
        g.mistakes_left -= 1
        if g.mistakes_left <= 0:
            await msg.reply_text("❌ Hết cơ hội cho cả nhóm. Ván dừng.")
            g.active = False
        return

    ok2, err2 = link_rule_ok(g.prev_tokens, toks)
    if not ok2:
        await msg.reply_text(err2)
        g.mistakes_left -= 1
        if g.mistakes_left <= 0:
            await msg.reply_text("❌ Hết cơ hội cho cả nhóm. Ván dừng.")
            g.active = False
        return

    key = _norm(" ".join(toks))
    if key in g.used:
        await msg.reply_text("⚠️ Cụm này dùng rồi, thử cái khác!")
        return

    # chấp nhận
    g.used.add(key)
    g.prev_tokens = toks
    await msg.reply_text(random.choice(OK_CHEERS))

    # Chế độ 1 người → bot đối lại
    if g.single_vs_bot:
        tail = toks[1]
        cand2 = pick_meaningful_word(tail, g.used)
        if not cand2:
            await context.bot.send_message(chat_id, "🤖 Bot bí rồi… bạn thắng!")
            g.active = False
            return
        bot_phrase = f"{tail} {cand2}"
        okb, _, toksb = is_valid_two_word_phrase(bot_phrase)
        if not okb:
            await context.bot.send_message(chat_id, "🤖 Bot bí rồi… bạn thắng!")
            g.active = False
            return
        g.used.add(_norm(bot_phrase))
        g.prev_tokens = toksb
        await context.bot.send_message(chat_id, f"🤖 {bot_phrase}")

    # Reset timers cho lượt kế tiếp
    schedule_timers(chat_id, context)

# =========================
# WRAPPER cho webhook.py
# =========================
class TGAppWrapper:
    def __init__(self):
        self.app = make_application()
    async def initialize(self):
        await self.app.initialize()
    async def start(self):
        await self.app.start()
    async def stop(self):
        await self.app.stop()
    async def shutdown(self):
        await self.app.shutdown()
    async def process_update(self, update: Update):
        await self.app.process_update(update)

def build_app():
    # webhook.py sẽ import hàm này
    return TGAppWrapper()
