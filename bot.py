# bot.py — Multi-Game (Đối Chữ + Đoán Chữ)
import os, re, json, random, time, logging
from collections import deque
from typing import Dict, List, Set, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from unidecode import unidecode

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, ApplicationBuilder, AIORateLimiter,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("multigame")

# ================== ENV & CONST ==================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# Lobby chung (cho cả 2 game)
AUTO_START_SECONDS = int(os.getenv("AUTO_START_SECONDS", "60"))   # đếm ngược sảnh
REMIND_EVERY_SECONDS = int(os.getenv("REMIND_EVERY_SECONDS", "30"))

# Thời gian mỗi lượt
TURN_SECONDS_WORDCHAIN = int(os.getenv("TURN_SECONDS_WORDCHAIN", "30"))
TURN_SECONDS_GUESS     = int(os.getenv("TURN_SECONDS_GUESS", "30"))

# Gist chung
GIST_ID    = os.getenv("GIST_ID", "").strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", "").strip()

# File riêng trong cùng 1 Gist (KHÔNG lẫn nhau)
GIST_DICT_FILE  = os.getenv("GIST_DICT_FILE",  "dict_offline.txt")     # Game Đối Chữ: lưu cụm đúng
GIST_GUESS_FILE = os.getenv("GIST_GUESS_FILE", "guess_clue_bank.json") # Game Đoán Chữ: ngân hàng câu (clue/answer)

# (tuỳ chọn) nguồn từ điển offline bổ sung
OFFLINE_DICT_URL  = os.getenv("OFFLINE_DICT_URL", "").strip()
OFFLINE_DICT_FILE = os.getenv("OFFLINE_DICT_FILE", "dict_vi.txt")

# Soha
SOHA_BASE = "http://tratu.soha.vn"

# Lệnh phụ
ONLY_PING_USER = "@yhck2"  # cho /iu Easter egg

# =========== TIỆN ÍCH CHUNG ===========
def normspace(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_two_words_vi(s: str) -> bool:
    s = normspace(s)
    parts = s.split(" ")
    return len(parts) == 2 and all(p for p in parts)

def first_last_word(s: str) -> Tuple[str, str]:
    s = normspace(s)
    a, b = s.split(" ")
    return a, b

def both_keys(s: str) -> Tuple[str, str]:
    s = normspace(s).lower()
    return s, unidecode(s)

def md_mention(uid: int, name: str) -> str:
    return f"[{name}](tg://user?id={uid})"

# =========== GIST I/O ===========
def _gist_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GIST_TOKEN:
        h["Authorization"] = f"token {GIST_TOKEN}"
    return h

def gist_read_file(filename: str) -> Optional[str]:
    if not GIST_ID:
        return None
    try:
        g = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=12).json()
        files = g.get("files", {})
        if filename in files and files[filename].get("content") is not None:
            return files[filename]["content"]
        # nếu không có "content" nhưng có raw_url
        if filename in files and files[filename].get("raw_url"):
            raw = files[filename]["raw_url"]
            r = requests.get(raw, timeout=12)
            if r.ok:
                return r.text
    except Exception as e:
        log.warning("gist_read_file(%s) error: %s", filename, e)
    return None

def gist_write_file(filename: str, content: str) -> bool:
    if not GIST_ID or not GIST_TOKEN:
        return False
    try:
        payload = {"files": {filename: {"content": content}}}
        r = requests.patch(f"https://api.github.com/gists/{GIST_ID}",
                           headers=_gist_headers(), data=json.dumps(payload), timeout=15)
        return r.ok
    except Exception as e:
        log.warning("gist_write_file(%s) error: %s", filename, e)
        return False

# =========== /start với 2 nút ===========
def start_keyboard():
    kb = [
        [
            InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="choose_wordchain"),
            InlineKeyboardButton("🧠 Game Đoán Chữ", callback_data="choose_guess"),
        ]
    ]
    return InlineKeyboardMarkup(kb)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Xin chào! Chọn trò bạn muốn chơi:",
        reply_markup=start_keyboard()
    )

# =========== TRẠNG THÁI CHỌN GAME & LOBBY ===========
# lưu game đã chọn gần nhất theo chat (để /newgame biết làm game nào)
LAST_GAME: Dict[int, str] = {}  # chat_id -> "wordchain"|"guess"

# lobby: chat_id -> state
LOBBY: Dict[int, Dict] = {}

async def on_choose_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat = update.effective_chat

    if q.data == "choose_wordchain":
        LAST_GAME[chat.id] = "wordchain"
        text = (
            "🎮 **Game Đối Chữ**\n"
            "• Luật: dùng **cụm 2 từ có nghĩa** (có dấu). Người sau **phải bắt đầu bằng từ cuối** của cụm trước.\n"
            f"• Mỗi lượt {TURN_SECONDS_WORDCHAIN}s, sai/không có nghĩa/hết giờ ⇒ bị loại.\n"
            "• 1 người tham gia → chơi một mình (BOT làm trọng tài).\n"
            "• ≥2 người → đấu với nhau, BOT làm trọng tài.\n\n"
            "Lệnh: /newgame (mở sảnh) • /join (tham gia) • /begin (bắt đầu) • /stop (dừng)"
        )
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        LAST_GAME[chat.id] = "guess"
        text = (
            "🧠 **Game Đoán Chữ** (ca dao, thành ngữ)\n"
            "• Mỗi người có **3 lượt đoán** theo vòng. Hết 3 lượt mà chưa đúng ⇒ **bị loại**.\n"
            f"• Mỗi lượt {TURN_SECONDS_GUESS}s; hết giờ tính như một lượt sai.\n"
            "• Chỉ cần 1 người cũng chơi được.\n"
            "• Không ai đoán đúng sau khi tất cả dùng hết lượt ⇒ kết thúc và công bố đáp án.\n\n"
            "Lệnh: /newgame (mở sảnh) • /join (tham gia) • /begin (bắt đầu) • /stop (dừng)"
        )
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# =========== LOBBY CHUNG ===========
async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    g = LAST_GAME.get(chat.id)
    if not g:
        await update.effective_message.reply_text("Hãy /start và chọn trò trước đã nhé.")
        return
    # reset lobby nếu có
    old = LOBBY.get(chat.id)
    if old:
        try:
            if old.get("count_job"): old["count_job"].schedule_removal()
            if old.get("rem_job"): old["rem_job"].schedule_removal()
        except: ...
        LOBBY.pop(chat.id, None)

    LOBBY[chat.id] = {
        "game": g,
        "players": set(),
        "created": time.time(),
        "count_job": context.job_queue.run_once(_auto_begin_job, when=AUTO_START_SECONDS, chat_id=chat.id),
        "rem_job": context.job_queue.run_repeating(_remind_job, interval=REMIND_EVERY_SECONDS,
                                                   first=REMIND_EVERY_SECONDS, chat_id=chat.id),
    }
    await update.effective_message.reply_text(
        f"🎮 Mở sảnh **{ 'Game Đối Chữ' if g=='wordchain' else 'Game Đoán Chữ' }**!\n"
        f"• /join để tham gia • tự bắt đầu sau {AUTO_START_SECONDS}s.",
        parse_mode=ParseMode.MARKDOWN
    )

async def _remind_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    st = LOBBY.get(chat_id)
    if not st: return
    remain = max(0, AUTO_START_SECONDS - int(time.time() - st["created"]))
    if remain <= 0: return
    msg = random.choice([
        "⏳ Mau mau /join nào!",
        "⌛ Sắp hết giờ chờ rồi!",
        "🕒 Lỡ sảnh là đợi ván sau nhé!",
        "📣 Gọi đồng đội vô chơi đi!",
        "🎲 Vào đông vui hơn mà!",
    ]) + f"\n🕰️ Còn {remain}s!"
    await ctx.application.bot.send_message(chat_id, msg)

async def _auto_begin_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    app = ctx.application
    st = LOBBY.get(chat_id)
    if not st: return
    # hủy job nhắc
    try:
        if st.get("rem_job"): st["rem_job"].schedule_removal()
    except: ...
    players = list(st["players"])
    game = st["game"]
    LOBBY.pop(chat_id, None)

    if len(players) == 0:
        await app.bot.send_message(chat_id, "⌛ Hết giờ mà chưa có ai /join. Đóng sảnh!")
        return

    if game == "wordchain":
        await start_wordchain(app, chat_id, players)
    else:
        await start_guess(app, chat_id, players)

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = LOBBY.get(chat_id)
    if not st:
        await update.effective_message.reply_text("❌ Chưa có sảnh. /start → chọn trò → /newgame.")
        return
    st["players"].add(update.effective_user.id)
    await update.effective_message.reply_text(
        f"✅ {update.effective_user.full_name} đã tham gia!"
    )

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = LOBBY.get(chat_id)
    if not st:
        await update.effective_message.reply_text("❌ Chưa có sảnh. /start → chọn trò → /newgame.")
        return
    try:
        if st.get("count_job"): st["count_job"].schedule_removal()
        if st.get("rem_job"): st["rem_job"].schedule_removal()
    except: ...
    players = list(st["players"])
    game = st["game"]
    LOBBY.pop(chat_id, None)
    if len(players) == 0:
        await update.effective_message.reply_text("⌛ Chưa có ai /join. Huỷ bắt đầu.")
        return
    if game == "wordchain":
        await start_wordchain(context.application, chat_id, players)
    else:
        await start_guess(context.application, chat_id, players)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # dọn lobby
    st = LOBBY.pop(chat_id, None)
    if st:
        try:
            if st.get("count_job"): st["count_job"].schedule_removal()
            if st.get("rem_job"): st["rem_job"].schedule_removal()
        except: ...
    # dọn game
    WORDCHAIN.pop(chat_id, None)
    GUESS.pop(chat_id, None)
    await update.effective_message.reply_text("🛑 Đã dừng ván / dọn sảnh.")

# =========== GAME 1: ĐỐI CHỮ ===========
# cache từ đúng / sai
DICT_OK: Set[str] = set()
DICT_BAD: Set[str] = set()

def load_offline_dict():
    seen = 0
    try:
        if OFFLINE_DICT_URL:
            r = requests.get(OFFLINE_DICT_URL, timeout=10)
            if r.ok:
                lines = r.text.splitlines()
            else:
                lines = []
        elif os.path.exists(OFFLINE_DICT_FILE):
            with open(OFFLINE_DICT_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []
        for ln in lines:
            w = normspace(ln).lower()
            if is_two_words_vi(w):
                DICT_OK.update([w, unidecode(w)])
                seen += 1
        log.info("Offline dict loaded: %d entries", seen)
    except Exception as e:
        log.warning("load_offline_dict err: %s", e)

def save_good_phrase_to_gist(phrase: str):
    """Append phrase vào GIST_DICT_FILE nếu chưa có (có dấu)."""
    if not (GIST_ID and GIST_TOKEN and GIST_DICT_FILE):
        return
    try:
        cur = gist_read_file(GIST_DICT_FILE) or ""
        lines = [l.strip().lower() for l in cur.splitlines() if l.strip()]
        p = normspace(phrase).lower()
        if p not in lines:
            new = (cur + ("\n" if cur and not cur.endswith("\n") else "") + phrase.strip() + "\n")
            gist_write_file(GIST_DICT_FILE, new)
    except Exception as e:
        log.warning("save_good_phrase_to_gist err: %s", e)

def _norm_vi(s: str) -> str:
    s = normspace(s).lower()
    s = re.sub(r"\s+", " ", s)
    return s

def soha_exact_match(phrase: str) -> bool:
    """Tra exact trên tratu.soha.vn (vn_vn)."""
    phrase = phrase.strip()
    if not phrase:
        return False

    headers = {"User-Agent": "Mozilla/5.0 (TelegramBot/wordchain)"}
    # 1) thử trang trực tiếp
    try:
        from urllib.parse import quote
        url = f"{SOHA_BASE}/dict/vn_vn/{quote(phrase, safe='')}"
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and r.text:
            soup = BeautifulSoup(r.text, "lxml")
            title = (soup.title.text if soup.title else "")
            if _norm_vi(title).startswith(_norm_vi(phrase)):
                return True
            # thử h1/h2/h3
            for tag in soup.find_all(["h1", "h2", "h3"]):
                t = _norm_vi(tag.get_text(" ", strip=True))
                if t == _norm_vi(phrase) or t.startswith(_norm_vi(phrase)):
                    return True
    except Exception:
        pass
    # 2) fallback search
    try:
        from urllib.parse import quote
        surl = f"{SOHA_BASE}/search.php?word={quote(phrase, safe='')}&dict=vn_vn"
        r = requests.get(surl, headers=headers, timeout=8)
        if r.status_code == 200 and r.text:
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "/dict/vn_vn/" in (a["href"] or ""):
                    txt = _norm_vi(a.get_text(" ", strip=True))
                    if txt == _norm_vi(phrase):
                        return True
    except Exception:
        pass
    return False

def is_valid_phrase(phrase: str) -> bool:
    phrase = normspace(phrase)
    if not is_two_words_vi(phrase):
        return False
    key_lc, key_no = both_keys(phrase)
    if key_lc in DICT_BAD or key_no in DICT_BAD:
        return False
    if key_lc in DICT_OK or key_no in DICT_OK:
        return True
    # online soha
    if soha_exact_match(phrase):
        DICT_OK.update([key_lc, key_no])
        save_good_phrase_to_gist(phrase)
        return True
    DICT_BAD.update([key_lc, key_no])
    return False

# Cà khịa (Đối chữ) ~15 câu
TAUNT_WORDCHAIN = [
    "Ôi trời, cụm này mà cũng dám xuất bản à? 😏",
    "Tra không ra luôn đó bạn ơi… về ôn lại chữ nghĩa nhé! 📚",
    "Sai như chưa từng sai! 🤣",
    "Cụm này Google còn bối rối nữa là mình 😅",
    "Bạn ơi, chữ với nghĩa giận bạn rồi đó! 🙃",
    "Cụm này nghe lạ tai phết… nhưng là sai nha! 🤭",
    "Không có nghĩa đâu, đừng cố chấp nữa bạn thân ơi 😌",
    "Chữ quốc ngữ khó quá thì mình chơi vần khác ha? 😜",
    "Cụm này gõ Soha nó cũng ngẩn người luôn! 🥲",
    "Sai rồi nha, đổi chiến thuật đi nè! 🧠",
    "Giằng co chi, sai là sai nha bạn! 😆",
    "Trật lất nghe chưa… thêm chất xám nào! 💡",
    "Ơ kìa, cụm này nhìn là thấy sai từ xa rồi! 🕵️",
    "Đừng làm từ điển khóc nữa! 😢",
    "Kiến thức là vô hạn, còn cụm này là vô nghĩa! 🌀",
]

class WordChainGame:
    def __init__(self, chat_id: int, players: List[int]):
        self.chat_id = chat_id
        self.players = deque(players)   # multi
        self.mode = "solo" if len(players) == 1 else "multi"
        self.current = self.players[0]
        self.tail: Optional[str] = None
        self.used: Set[str] = set()
        self.turn_job = None

    def rotate(self):
        self.players.rotate(-1)
        self.current = self.players[0]

WORDCHAIN: Dict[int, WordChainGame] = {}

async def start_wordchain(app: Application, chat_id: int, players: List[int]):
    random.shuffle(players)
    gs = WordChainGame(chat_id, players)
    WORDCHAIN[chat_id] = gs
    if gs.mode == "solo":
        await app.bot.send_message(
            chat_id,
            f"🧍 **Game Đối Chữ** (solo)\n"
            f"• Gửi **cụm 2 từ có nghĩa** bất kỳ.\n"
            f"• Lượt sau phải bắt đầu bằng **từ cuối** của cụm trước.\n"
            f"• Mỗi lượt {TURN_SECONDS_WORDCHAIN}s.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await app.bot.send_message(
            chat_id,
            f"👥 **Game Đối Chữ** (nhiều người)\n"
            f"• Người mở màn: {md_mention(gs.current, 'người này')}\n"
            f"• Gửi **cụm 2 từ có nghĩa**, sau đó đối bằng **từ cuối**.\n"
            f"• Mỗi lượt {TURN_SECONDS_WORDCHAIN}s.",
            parse_mode=ParseMode.MARKDOWN
        )
    await announce_wordchain_turn(app, gs)

async def announce_wordchain_turn(app: Application, gs: WordChainGame):
    if gs.mode == "solo":
        msg = "✨ Gửi **cụm 2 từ có nghĩa**" + (f" (bắt đầu bằng **{gs.tail}**)" if gs.tail else "") + "."
        await app.bot.send_message(gs.chat_id, msg, parse_mode=ParseMode.MARKDOWN)
    else:
        msg = (f"🎯 Lượt của {md_mention(gs.current, 'người này')}"
               + (f" — bắt đầu bằng **{gs.tail}**." if gs.tail else " — mở màn, gửi cụm bất kỳ."))
        await app.bot.send_message(gs.chat_id, msg, parse_mode=ParseMode.MARKDOWN)
    await schedule_wordchain_timers(app, gs)

async def schedule_wordchain_timers(app: Application, gs: WordChainGame):
    # huỷ job cũ
    try:
        if gs.turn_job: gs.turn_job.schedule_removal()
    except: ...
    # nhắc 5s còn lại + timeout
    async def remind(ctx: ContextTypes.DEFAULT_TYPE):
        await app.bot.send_message(gs.chat_id, "⏰ Còn 5 giây!")
    async def timeout(ctx: ContextTypes.DEFAULT_TYPE):
        if gs.mode == "multi":
            kicked = gs.current
            await app.bot.send_message(gs.chat_id,
                f"⏱️ Hết giờ! {md_mention(kicked, 'người này')} bị loại.",
                parse_mode=ParseMode.MARKDOWN)
            try: gs.players.remove(kicked)
            except: ...
            if len(gs.players) <= 1:
                if len(gs.players) == 1:
                    await app.bot.send_message(gs.chat_id, f"🏆 {md_mention(gs.players[0],'người này')} vô địch!",
                                               parse_mode=ParseMode.MARKDOWN)
                else:
                    await app.bot.send_message(gs.chat_id, "🏁 Không còn người chơi. Kết thúc ván.")
                WORDCHAIN.pop(gs.chat_id, None); return
            gs.current = gs.players[0]
            await announce_wordchain_turn(app, gs)
        else:
            await app.bot.send_message(gs.chat_id, "⏱️ Hết giờ! Kết thúc ván (solo).")
            WORDCHAIN.pop(gs.chat_id, None)

    app.job_queue.run_once(remind, when=TURN_SECONDS_WORDCHAIN-5, chat_id=gs.chat_id)
    gs.turn_job = app.job_queue.run_once(timeout, when=TURN_SECONDS_WORDCHAIN, chat_id=gs.chat_id)

def _wc_fail_reason(phrase: str, gs: WordChainGame) -> Optional[str]:
    phrase = normspace(phrase)
    if not is_two_words_vi(phrase):
        return "Câu phải gồm **2 từ**."
    if gs.tail:
        a, b = first_last_word(phrase)
        if a.lower() != gs.tail.lower():
            return f"Câu phải bắt đầu bằng **{gs.tail}**."
    if phrase.lower() in gs.used:
        return "Cụm đã dùng rồi."
    if not is_valid_phrase(phrase):
        return "Cụm không có nghĩa trên từ điển."
    return None

async def on_text_wordchain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    gs = WORDCHAIN.get(chat.id)
    if not gs: return

    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()

    # multi: chỉ người đến lượt
    if gs.mode == "multi" and uid != gs.current:
        return

    reason = _wc_fail_reason(text, gs)
    if reason:
        # cà khịa + loại
        taunt = random.choice(TAUNT_WORDCHAIN)
        if gs.mode == "multi":
            await update.effective_message.reply_text(
                f"{taunt}\n❌ {reason}\n➡️ {md_mention(uid,'bạn')} bị loại.",
                parse_mode=ParseMode.MARKDOWN
            )
            try: gs.players.remove(uid)
            except: ...
            if len(gs.players) <= 1:
                if len(gs.players) == 1:
                    await context.bot.send_message(chat.id, f"🏆 {md_mention(gs.players[0],'người này')} vô địch!",
                                                   parse_mode=ParseMode.MARKDOWN)
                else:
                    await context.bot.send_message(chat.id, "🏁 Không còn người chơi. Kết thúc ván.")
                WORDCHAIN.pop(chat.id, None); return
            gs.current = gs.players[0]
            await announce_wordchain_turn(context.application, gs)
        else:
            await update.effective_message.reply_text(f"{taunt}\n👑 Kết thúc ván (solo).",
                                                      parse_mode=ParseMode.MARKDOWN)
            WORDCHAIN.pop(chat.id, None)
        return

    # hợp lệ
    gs.used.add(text.lower())
    _, tail = first_last_word(text)
    gs.tail = tail
    await update.effective_message.reply_text("✅ Hợp lệ, tiếp tục!", parse_mode=ParseMode.MARKDOWN)

    if gs.mode == "multi":
        gs.rotate()
    await announce_wordchain_turn(context.application, gs)

# =========== GAME 2: ĐOÁN CHỮ (ca dao/ thành ngữ) ===========
# cấu trúc câu hỏi: {"clue": "...", "answer": "..."}
DEFAULT_CLUES = [
    {"clue": "Ăn quả nhớ kẻ trồng cây (điền 4 chữ)", "answer": "uống nước nhớ nguồn"},
    {"clue": "Một cây làm chẳng nên non, ... (hoàn thiện câu)", "answer": "ba cây chụm lại nên hòn núi cao"},
    {"clue": "Điền tục ngữ về học tập: 'Có công mài sắt ...'", "answer": "có ngày nên kim"},
]

def load_guess_bank() -> List[Dict[str,str]]:
    txt = gist_read_file(GIST_GUESS_FILE)
    if not txt:
        # nếu chưa có, tạo mặc định
        gist_write_file(GIST_GUESS_FILE, json.dumps(DEFAULT_CLUES, ensure_ascii=False, indent=2))
        return DEFAULT_CLUES.copy()
    try:
        data = json.loads(txt)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and "clue" in x and "answer" in x]
        return DEFAULT_CLUES.copy()
    except Exception:
        return DEFAULT_CLUES.copy()

def save_guess_bank(bank: List[Dict[str,str]]):
    gist_write_file(GIST_GUESS_FILE, json.dumps(bank, ensure_ascii=False, indent=2))

def equal_answer(a: str, b: str) -> bool:
    # So sánh không phân biệt hoa/thường & không dấu & bỏ khoảng trắng thừa
    def norm(x: str):
        return re.sub(r"\s+"," ", unidecode(x.strip().lower()))
    return norm(a) == norm(b)

# cà khịa đoán chữ
TAUNT_GUESS = [
    "Trượt nhẹ thôi mà đau cả lòng 😆",
    "Ơ kìa, đoán mù à bạn ơi? 🙄",
    "Gần đúng… ở vũ trụ song song 🤭",
    "Câu này mà cũng hụt thì thôi xin luôn! 😂",
    "Sai rồi nè, đừng cay cú nha! 😜",
    "Thêm xíu muối i-ốt cho não nào! 🧂🧠",
    "Đoán hên xui hả? Hơi xui đó! 🍀",
    "Bạn ơi, không phải đâu nha~ 😝",
    "Ơn giời, câu sai đây rồi! 🤡",
    "Hụt mất rồi, làm ván nữa không? 🎲",
    "Sai mất rồi, tập trung nào! 🔎",
    "Lệch kha khá đó bạn ơi! 🧭",
    "Không phải đáp án, thử hướng khác xem! 🧩",
    "Ấm ớ hội tề quá nha! 😅",
    "Trật lất rồi… nhưng vẫn đáng yêu! 💖",
]

class GuessGame:
    def __init__(self, chat_id: int, players: List[int], bank: List[Dict[str,str]]):
        self.chat_id = chat_id
        self.players = deque(players) if players else deque([])
        self.turn_seconds = TURN_SECONDS_GUESS
        # mỗi người có 3 lượt
        self.remain: Dict[int,int] = {pid: 3 for pid in players} if players else {}
        self.current: Optional[int] = self.players[0] if players else None
        self.bank = bank
        self.used_idx: Set[int] = set()
        self.q_idx: Optional[int] = None
        self.turn_job = None

    def next_player(self):
        # bỏ những ai hết lượt (0) ra khỏi vòng
        while self.players and self.remain.get(self.players[0],0) <= 0:
            self.players.popleft()
        if not self.players:
            self.current = None
            return
        self.players.rotate(-1)
        while self.players and self.remain.get(self.players[0],0) <= 0:
            self.players.popleft()
        self.current = self.players[0] if self.players else None

GUESS: Dict[int, GuessGame] = {}

def pick_new_question(gs: GuessGame) -> bool:
    # lấy ngẫu nhiên câu chưa dùng
    idxs = [i for i in range(len(gs.bank)) if i not in gs.used_idx]
    if not idxs:
        return False
    gs.q_idx = random.choice(idxs)
    gs.used_idx.add(gs.q_idx)
    return True

async def start_guess(app: Application, chat_id: int, players: List[int]):
    bank = load_guess_bank()
    gs = GuessGame(chat_id, players, bank)
    GUESS[chat_id] = gs

    await app.bot.send_message(
        chat_id,
        f"🧠 **Game Đoán Chữ**\n"
        f"• Mỗi người có **3 lượt đoán** theo vòng. Hết 3 lượt mà chưa đúng ⇒ bị loại.\n"
        f"• Mỗi lượt {TURN_SECONDS_GUESS}s; hết giờ tính như 1 lần sai.\n"
        f"• Nếu tất cả hết lượt mà không ai đúng ⇒ công bố đáp án và kết thúc.",
        parse_mode=ParseMode.MARKDOWN
    )
    if not pick_new_question(gs):
        await app.bot.send_message(chat_id, "Không còn câu hỏi trong ngân hàng. Hãy bổ sung vào Gist!")
        GUESS.pop(chat_id, None); return

    clue = gs.bank[gs.q_idx]["clue"]
    await app.bot.send_message(chat_id, f"❓ Câu hỏi: **{clue}**", parse_mode=ParseMode.MARKDOWN)
    await announce_guess_turn(app, gs)

async def announce_guess_turn(app: Application, gs: GuessGame):
    # Chọn người hiện tại (bỏ ai hết lượt)
    while gs.players and gs.remain.get(gs.players[0],0) <= 0:
        gs.players.popleft()
    if not gs.players:
        # tất cả hết lượt → công bố đáp án
        ans = gs.bank[gs.q_idx]["answer"] if gs.q_idx is not None else "(không có)"
        await app.bot.send_message(gs.chat_id, f"🏁 Hết lượt mọi người.\n🔎 Đáp án: **{ans}**",
                                   parse_mode=ParseMode.MARKDOWN)
        GUESS.pop(gs.chat_id, None)
        return

    gs.current = gs.players[0]
    await app.bot.send_message(
        gs.chat_id,
        f"🎯 Lượt của {md_mention(gs.current,'bạn')} — bạn còn **{gs.remain.get(gs.current,0)}** lượt.",
        parse_mode=ParseMode.MARKDOWN
    )
    await schedule_guess_timers(app, gs)

async def schedule_guess_timers(app: Application, gs: GuessGame):
    # huỷ job cũ
    try:
        if gs.turn_job: gs.turn_job.schedule_removal()
    except: ...
    async def remind(ctx: ContextTypes.DEFAULT_TYPE):
        await app.bot.send_message(gs.chat_id, "⏰ Còn 5 giây!")
    async def timeout(ctx: ContextTypes.DEFAULT_TYPE):
        # hết giờ: trừ 1 lượt
        if gs.current is None:
            return
        gs.remain[gs.current] = max(0, gs.remain.get(gs.current,0) - 1)
        await app.bot.send_message(gs.chat_id,
            f"⏱️ Hết giờ! {md_mention(gs.current,'bạn')} mất 1 lượt (còn {gs.remain[gs.current]}).",
            parse_mode=ParseMode.MARKDOWN
        )
        # chuyển lượt
        gs.next_player()
        await announce_guess_turn(app, gs)

    app.job_queue.run_once(remind, when=TURN_SECONDS_GUESS-5, chat_id=gs.chat_id)
    gs.turn_job = app.job_queue.run_once(timeout, when=TURN_SECONDS_GUESS, chat_id=gs.chat_id)

async def on_text_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    gs = GUESS.get(chat.id)
    if not gs: return

    uid = update.effective_user.id
    if uid != gs.current:
        return  # không phải lượt của bạn

    text = (update.effective_message.text or "").strip()
    if not text:
        return
    # chấm
    ans = gs.bank[gs.q_idx]["answer"] if gs.q_idx is not None else ""
    if equal_answer(text, ans):
        await update.effective_message.reply_text(
            f"🎉 Chính xác! {md_mention(uid,'bạn')} trả lời đúng!\n🏁 Kết thúc câu!",
            parse_mode=ParseMode.MARKDOWN
        )
        GUESS.pop(chat.id, None)
        return

    # sai → trừ lượt + cà khịa
    gs.remain[uid] = max(0, gs.remain.get(uid,0) - 1)
    taunt = random.choice(TAUNT_GUESS)
    await update.effective_message.reply_text(
        f"{taunt}\n❌ Sai rồi! Bạn còn **{gs.remain[uid]}** lượt.",
        parse_mode=ParseMode.MARKDOWN
    )
    # hết lượt người này → loại khỏi vòng
    if gs.remain[uid] <= 0:
        await context.bot.send_message(chat.id, f"⛔ {md_mention(uid,'bạn')} đã dùng hết lượt và bị loại.",
                                       parse_mode=ParseMode.MARKDOWN)
    # chuyển lượt
    gs.next_player()
    await announce_guess_turn(context.application, gs)

# =========== LỆNH PHỤ ===========
async def cmd_iu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.username:
        if ("@" + update.effective_user.username).lower() == ONLY_PING_USER.lower():
            await update.effective_message.reply_text("Anh Nam Yêu Em Thiệu ❤️"); return
    await update.effective_message.reply_text("iu gì mà iu 😏")

# =========== ROUTING TEXT ===========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # route text vào game tương ứng nếu đang chơi
    chat_id = update.effective_chat.id
    if chat_id in WORDCHAIN:
        await on_text_wordchain(update, context)
    elif chat_id in GUESS:
        await on_text_guess(update, context)
    else:
        # không trong ván nào → bỏ qua
        pass

# =========== INIT / BUILD ===========
async def initialize(app: Application):
    # nạp offline dict cho Đối Chữ
    load_offline_dict()
    # nạp bank đoán chữ (nếu rỗng thì tạo mặc định)
    _ = load_guess_bank()
    log.info("Initialized.")

async def stop(app: Application):
    pass

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    # start + chọn game
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_choose_game, pattern="^choose_(wordchain|guess)$"))

    # lobby chung
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler(["stop","ketthuc"], cmd_stop))

    # lệnh vui
    app.add_handler(CommandHandler("iu", cmd_iu))

    # route text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
