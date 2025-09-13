import os, re, json, asyncio, logging, random
from typing import Dict, List, Optional, Set
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Message, User
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, JobQueue
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("dochoi")

BOT_TOKEN   = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
GIST_ID     = os.environ["GIST_ID"]
GIST_TOKEN  = os.environ["GIST_TOKEN"]
DICT_FILE   = os.getenv("DICT_FILE", "dict_offline.txt")
GUESS_FILE  = os.getenv("GUESS_FILE", "guess_clue_bank.json")

# -----------------------------
# GIST helpers (pure GitHub API)
# -----------------------------
GH_API = "https://api.github.com"

async def gist_get_all(context: ContextTypes.DEFAULT_TYPE) -> Dict:
    headers = {"Authorization": f"Bearer {GIST_TOKEN}",
               "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{GH_API}/gists/{GIST_ID}", headers=headers)
        r.raise_for_status()
        return r.json()

async def gist_get_file(filename: str) -> str:
    headers = {"Authorization": f"Bearer {GIST_TOKEN}",
               "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{GH_API}/gists/{GIST_ID}", headers=headers)
        r.raise_for_status()
        data = r.json()["files"].get(filename)
        if not data:
            return ""
        if data.get("truncated"):
            raw_url = data["raw_url"]
            r2 = await client.get(raw_url)
            r2.raise_for_status()
            return r2.text
        return data.get("content", "")

async def gist_save_file(filename: str, content: str) -> None:
    headers = {"Authorization": f"Bearer {GIST_TOKEN}",
               "Accept": "application/vnd.github+json"}
    payload = {"files": {filename: {"content": content}}}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(f"{GH_API}/gists/{GIST_ID}", headers=headers, json=payload)
        r.raise_for_status()

# -----------------------------
# Utilities
# -----------------------------
def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def norm_key(s: str) -> str:
    # khóa so khớp nhẹ: lowercase + rút gọn khoảng trắng
    return normalize_spaces(s).lower()

# -----------------------------
# OFFLINE DICTIONARY CACHE
# -----------------------------
class DictCache:
    def __init__(self):
        self.raw: Set[str] = set()     # giữ nguyên có dấu (dùng lưu)
        self.keys: Set[str] = set()    # key normalize để tra nhanh
        self.loaded = False

    async def load(self):
        if self.loaded:
            return
        try:
            txt = await gist_get_file(DICT_FILE)
            if not txt.strip():
                txt = "[]"
            data = json.loads(txt)
            for phrase in data:
                k = norm_key(phrase)
                self.raw.add(phrase)
                self.keys.add(k)
            self.loaded = True
            log.info("Loaded %d phrases from gist", len(self.raw))
        except Exception as e:
            log.exception("Load dict failed: %s", e)
            self.loaded = True  # vẫn cho chạy

    async def persist(self):
        try:
            data = sorted(self.raw)
            await gist_save_file(DICT_FILE, json.dumps(data, ensure_ascii=False, indent=2))
            log.info("Persisted dict: %d items", len(data))
        except Exception as e:
            log.exception("Persist dict failed: %s", e)

    def has(self, phrase: str) -> bool:
        return norm_key(phrase) in self.keys

    def add(self, phrase: str):
        if not self.has(phrase):
            self.raw.add(phrase)
            self.keys.add(norm_key(phrase))

DICT = DictCache()

# -----------------------------
# ONLINE CHECKERS
# -----------------------------
NEG_MARKERS = [
    "không tìm thấy", "không có kết quả", "rất tiếc", "404", "not found"
]

async def check_soha(phrase: str) -> bool:
    """
    Kiểm tra nhanh trên tratu.soha.vn (không dùng lxml).
    Hợp lệ nếu HTTP 200 và trang không chứa các cụm "không tìm thấy".
    """
    url = f"http://tratu.soha.vn/dict/vn_vn/{quote_plus(phrase)}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        text = r.text.lower()
        if any(bad in text for bad in NEG_MARKERS):
            return False
        # một số kiểm tra nhẹ bằng BS4 (parser thuần Python)
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.title.get_text() if soup.title else "").lower()
        if phrase.lower() in title:
            return True
        # Nếu có khối nội dung từ điển/dịch nghĩa → coi như hợp lệ
        if soup.find(id="content-tdict") or soup.find(class_=re.compile("detail|explain|mean", re.I)):
            return True
        # fallback: trang dài có chữ 'từ điển' cũng tạm coi hợp lệ
        if "từ điển" in text and len(text) > 2000:
            return True
        return False

async def check_wiktionary(phrase: str) -> bool:
    url = f"https://vi.wiktionary.org/wiki/{quote_plus(phrase.replace(' ', '_'))}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        t = r.text.lower()
        if any(bad in t for bad in ["trang này không tồn tại", "không tìm thấy"]):
            return False
        return len(t) > 1500

async def phrase_is_valid(phrase: str) -> bool:
    phrase = normalize_spaces(phrase)
    # phải đúng 2 từ
    parts = phrase.split(" ")
    if len(parts) != 2:
        return False
    # tra offline
    if DICT.has(phrase):
        return True
    # tra online (Soha trước, rồi Wiktionary)
    ok = await check_soha(phrase)
    if not ok:
        ok = await check_wiktionary(phrase)
    if ok:
        DICT.add(phrase)
        # lưu không đồng bộ (không chặn lượt chơi)
        asyncio.create_task(DICT.persist())
    return ok

# -----------------------------
# GAME STATE
# -----------------------------
TAUNTS_DOICHU = [
    "Sai rồi nha! Động não lại nào 🤯",
    "Không ổn! Cụm này tớ chưa thấy trong từ điển 😅",
    "Trượt rồi, thử câu gọn gàng hơn xem?",
    "Không hợp lệ – kiếm cụm có nghĩa nha!",
    "Hơi gượng ép đó… cho tớ cụm chuẩn hơn!",
    "Cụm này lạ quá, từ điển bó tay 😵‍💫",
    "Chưa được đâu, thử lại đi chiến hữu!",
    "Ối dồi, chưa đúng! Đổi bài nha!",
    "Không qua vòng gửi xe 🚫",
    "Cụm chuẩn nghĩa mới tính điểm nha!"
]

TAUNTS_DOAN = [
    "Sai mất rồi 😝", "Không phải đáp án đâu!", "Hụt rồi nha!",
    "Gần đúng… nhưng không phải 😆", "Trật lất!", "Hơi lệch pha!",
    "Thử hướng khác xem 👀", "Không đúng, cố lên!",
    "Đáp án vẫn ẩn…", "Nope!", "Sai mất tiêu!",
    "Lệch sóng 📡", "Chưa phải, đừng nản!", "Chệch một xíu!",
    "Ố la la – chưa đúng!"
]

class DoiChuRoom:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.players: List[User] = []
        self.alive: List[int] = []      # user_id còn sống
        self.current_idx = 0
        self.last_word: Optional[str] = None
        self.message_id_rules: Optional[int] = None
        self.is_vs_bot = False
        self.turn_job = None

class DoanChuRoom:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.players: List[User] = []
        self.turn_idx = 0
        self.guess_left: Dict[int, int] = {}
        self.qa = None  # {"q":, "a":, "hints":[]}
        self.turn_job = None

# Mỗi chat một state
def gc(chat_data: dict) -> dict:
    if "state" not in chat_data:
        chat_data["state"] = {"mode": None, "room": None, "lobby": None}
    return chat_data["state"]

# -----------------------------
# GIST câu hỏi đoán chữ
# -----------------------------
async def load_guess_bank() -> List[dict]:
    txt = await gist_get_file(GUESS_FILE)
    if not txt.strip():
        return []
    try:
        return json.loads(txt)
    except Exception:
        return []

async def append_guess_item(item: dict):
    bank = await load_guess_bank()
    bank.append(item)
    await gist_save_file(GUESS_FILE, json.dumps(bank, ensure_ascii=False, indent=2))

# -----------------------------
# UI /start
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="menu_doi"),
        InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="menu_doan")
    ]])
    await update.effective_chat.send_message(
        "Chọn trò nào nè 👇",
        reply_markup=kb
    )

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data
    state = gc(context.chat_data)
    state["mode"] = None
    state["room"] = None
    state["lobby"] = {"players": []}

    if mode == "menu_doi":
        text = ("🎮 *ĐỐI CHỮ*\n"
                "• Đối *cụm 2 từ có nghĩa*. Lượt sau phải bắt đầu bằng *từ cuối* của cụm trước.\n"
                "• Mở sảnh 60s. /join để tham gia.\n"
                "• Mỗi lượt 30s. Sai/không hợp lệ/het giờ ⇒ loại.")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Mở sảnh (newgame)", callback_data="doi_new"),
            InlineKeyboardButton("🔑 Tham gia (join)", callback_data="doi_join")
        ]])
        msg = await q.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        text = ("🧩 *ĐOÁN CHỮ*\n"
                "• Random câu hỏi (ca dao/thành ngữ...).\n"
                "• Mỗi người *3 lượt đoán*. Hết lượt trước ⇒ bị loại.\n"
                "• /join để tham gia. Bắt đầu sau 60s.")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Mở sảnh (newgame)", callback_data="doan_new"),
            InlineKeyboardButton("🔑 Tham gia (join)", callback_data="doan_join")
        ]])
        msg = await q.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    state["rule_message_id"] = msg.message_id

# -----------------------------
# Lobby & Join
# -----------------------------
LOBBY_SECONDS = 60
TURN_SECONDS  = 30

async def lobby_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    kind    = context.job.data["kind"]
    state   = gc(context.chat_data)
    players = state["lobby"]["players"]

    if not players:
        await context.bot.send_message(chat_id, "⏰ Hết 60s nhưng chưa ai /join. Hủy sảnh.")
        state["lobby"] = None
        return

    if kind == "doi":
        room = DoiChuRoom(chat_id)
        room.players = players.copy()
        room.alive   = [u.id for u in room.players]
        room.is_vs_bot = (len(room.players) == 1)
        state["mode"] = "doi"
        state["room"] = room
        await context.bot.send_message(chat_id,
            f"🔔 Bắt đầu! {len(room.players)} người tham gia. " +
            ("Chơi với BOT." if room.is_vs_bot else "BOT chỉ làm trọng tài.")
        )
        # chọn người bắt đầu
        room.current_idx = 0 if room.is_vs_bot else random.randrange(len(room.players))
        room.last_word = None
        await announce_next_turn_doi(context, room)
    else:
        bank = await load_guess_bank()
        if not bank:
            await context.bot.send_message(chat_id, "Chưa có câu hỏi trong ngân hàng (guess_clue_bank.json).")
            state["lobby"] = None
            return
        room = DoanChuRoom(chat_id)
        room.players = players.copy()
        for u in room.players:
            room.guess_left[u.id] = 3
        room.qa = random.choice(bank)
        state["mode"] = "doan"
        state["room"] = room
        await context.bot.send_message(chat_id,
            f"🔔 Bắt đầu! {len(room.players)} người tham gia.\n"
            f"❓ Câu hỏi: *{room.qa.get('question','')}*\n"
            f"💡 Gợi ý: {', '.join(room.qa.get('hints', [])) if room.qa.get('hints') else '—'}",
            parse_mode="Markdown"
        )
        await announce_next_turn_doan(context, room)

async def handle_new_join(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    q = update.callback_query
    await q.answer()
    state = gc(context.chat_data)
    if state.get("room") or not state.get("lobby"):
        state["lobby"] = {"players": []}
    # thêm người
    user = q.from_user
    players: List[User] = state["lobby"]["players"]
    if user.id not in [u.id for u in players]:
        players.append(user)
        await q.message.reply_text(f"✅ {user.mention_html()} đã tham gia!", parse_mode="HTML")

async def on_doi_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = gc(context.chat_data)
    state["lobby"] = {"players": []}
    await update.callback_query.answer()
    await update.effective_chat.send_message("🎮 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau 60s nếu có người tham gia.")
    # đặt hẹn giờ
    context.job_queue.run_once(lobby_tick, when=LOBBY_SECONDS, chat_id=update.effective_chat.id,
                               name=f"lobby_doi_{update.effective_chat.id}",
                               data={"kind": "doi"})

async def on_doan_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = gc(context.chat_data)
    state["lobby"] = {"players": []}
    await update.callback_query.answer()
    await update.effective_chat.send_message("🧩 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau 60s nếu có người tham gia.")
    context.job_queue.run_once(lobby_tick, when=LOBBY_SECONDS, chat_id=update.effective_chat.id,
                               name=f"lobby_doan_{update.effective_chat.id}",
                               data={"kind": "doan"})

async def on_join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = gc(context.chat_data)
    if not state.get("lobby"):
        await update.message.reply_text("Chưa mở sảnh. Ấn /start để chọn game rồi bấm *Mở sảnh*.", parse_mode="Markdown")
        return
    players: List[User] = state["lobby"]["players"]
    user = update.effective_user
    if user.id not in [u.id for u in players]:
        players.append(user)
        await update.message.reply_text(f"✅ {user.mention_html()} đã tham gia!", parse_mode="HTML")

# -----------------------------
# ĐỐI CHỮ – vòng chơi
# -----------------------------
async def announce_next_turn_doi(context: ContextTypes.DEFAULT_TYPE, room: DoiChuRoom):
    # dừng job cũ
    if room.turn_job:
        room.turn_job.schedule_removal()
    # loại người đã bị loại
    room.alive = [uid for uid in room.alive if uid in [u.id for u in room.players]]
    if room.is_vs_bot and room.alive and room.alive[0] != room.players[0].id:
        room.alive = [room.players[0].id]

    if not room.alive:
        await context.bot.send_message(room.chat_id, "Hết người chơi. Kết thúc ván.")
        gc(context.chat_data)["room"] = None
        return

    # xác định người tới lượt
    if room.current_idx >= len(room.players):
        room.current_idx = 0
    cur_user = room.players[room.current_idx]
    if cur_user.id not in room.alive:
        # chuyển tới người kế
        room.current_idx = (room.current_idx + 1) % len(room.players)
        await announce_next_turn_doi(context, room)
        return

    need = f"*{room.last_word}*" if room.last_word else "bất kỳ"
    await context.bot.send_message(
        room.chat_id,
        f"👉 {cur_user.mention_html()} tới lượt. Gửi *cụm 2 từ có nghĩa* (bắt đầu bằng {need}).",
        parse_mode="HTML"
    )
    # đặt timer nhắc + hết giờ
    room.turn_job = context.job_queue.run_once(doi_turn_timeout, TURN_SECONDS, chat_id=room.chat_id,
                                               data={"uid": cur_user.id})

async def doi_turn_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    uid = context.job.data["uid"]
    state = gc(context.chat_data)
    room: DoiChuRoom = state.get("room")
    if not room or state.get("mode") != "doi": 
        return
    if uid in room.alive:
        room.alive.remove(uid)
        await context.bot.send_message(chat_id, "⏰ Hết giờ! Bị loại.")
    # chuyển lượt
    room.current_idx = (room.current_idx + 1) % len(room.players)
    await announce_next_turn_doi(context, room)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Bắt mọi tin nhắn text trong chat – kiểm tra trò đang chạy và xử lý.
    """
    state = gc(context.chat_data)
    mode  = state.get("mode")
    if not mode or not state.get("room"):
        return

    text = normalize_spaces(update.message.text)
    user = update.effective_user

    # -------- ĐỐI CHỮ --------
    if mode == "doi":
        room: DoiChuRoom = state["room"]
        if user.id != room.players[room.current_idx].id:
            return  # không tới lượt

        # kiểm tra 2 từ & chữ cái đầu nếu có last_word
        parts = text.split(" ")
        if len(parts) != 2:
            await update.message.reply_text("❌ Cần *đúng 2 từ*. Thử lại nhé.", parse_mode="Markdown")
            return
        if room.last_word and parts[0].lower() != room.last_word.lower():
            await update.message.reply_text(
                f"❌ Sai luật. Cụm phải bắt đầu bằng **{room.last_word}**.", parse_mode="Markdown"
            )
            return

        ok = await phrase_is_valid(text)
        if not ok:
            await update.message.reply_text(f"❌ {random.choice(TAUNTS_DOICHU)}\n*Cụm không có nghĩa* (không tìm thấy).",
                                            parse_mode="Markdown")
            # loại
            if user.id in room.alive:
                room.alive.remove(user.id)
            room.current_idx = (room.current_idx + 1) % len(room.players)
            await announce_next_turn_doi(context, room)
            return

        # câu hợp lệ → cập nhật, chuyển lượt
        room.last_word = parts[1]
        # nếu chơi với BOT → BOT đáp
        if room.is_vs_bot:
            await update.message.reply_text("✅ Hợp lệ. Đến BOT…")
            await asyncio.sleep(1.2)
            # BOT chọn cụm bất kỳ bắt đầu bằng last_word
            candidate = None
            for p in sorted(DICT.raw):
                ps = p.split(" ")
                if len(ps) == 2 and ps[0].lower() == room.last_word.lower():
                    candidate = p
                    break
            if candidate:
                await context.bot.send_message(room.chat_id, f"🤖 BOT: {candidate}")
                room.last_word = candidate.split(" ")[1]
                await announce_next_turn_doi(context, room)
            else:
                await context.bot.send_message(room.chat_id, "🤖 BOT bí rồi. Bạn thắng!")
                gc(context.chat_data)["room"] = None
            return

        # nhiều người chơi
        room.current_idx = (room.current_idx + 1) % len(room.players)
        await announce_next_turn_doi(context, room)
        return

    # -------- ĐOÁN CHỮ --------
    if mode == "doan":
        room: DoanChuRoom = state["room"]
        cur = room.players[room.turn_idx]
        if user.id != cur.id:
            return
        # so khớp không phân biệt hoa thường & bỏ khoảng trắng thừa
        ans = normalize_spaces(room.qa.get("answer", ""))
        if norm_key(text) == norm_key(ans):
            await update.message.reply_text(f"🎉 Chính xác! *{ans}*", parse_mode="Markdown")
            gc(context.chat_data)["room"] = None
            return
        # sai → trừ lượt
        room.guess_left[user.id] -= 1
        left = room.guess_left[user.id]
        await update.message.reply_text(f"❌ {random.choice(TAUNTS_DOAN)}  (còn {left} lượt)")
        if left <= 0:
            await update.message.reply_text("🚫 Hết lượt – bị loại.")
            room.players = [u for u in room.players if u.id != user.id]
            if not room.players:
                await update.message.reply_text("Hết người chơi. Kết thúc ván.")
                gc(context.chat_data)["room"] = None
                return
            # cập nhật con trỏ
            room.turn_idx %= len(room.players)
        else:
            # chuyển người tiếp theo
            room.turn_idx = (room.turn_idx + 1) % len(room.players)
        await announce_next_turn_doan(context, room)

async def announce_next_turn_doan(context: ContextTypes.DEFAULT_TYPE, room: DoanChuRoom):
    if room.turn_job:
        room.turn_job.schedule_removal()
    if not room.players:
        gc(context.chat_data)["room"] = None
        return
    cur = room.players[room.turn_idx]
    await context.bot.send_message(room.chat_id, f"👉 {cur.mention_html()} tới lượt đoán.", parse_mode="HTML")
    room.turn_job = context.job_queue.run_once(doan_turn_timeout, TURN_SECONDS, chat_id=room.chat_id,
                                               data={"uid": cur.id})

async def doan_turn_timeout(context: ContextTypes.DEFAULT_TYPE):
    state = gc(context.chat_data)
    room: DoanChuRoom = state.get("room")
    if not room or state.get("mode") != "doan":
        return
    cur = room.players[room.turn_idx]
    if room.guess_left.get(cur.id, 0) > 0:
        room.guess_left[cur.id] -= 1
        await context.bot.send_message(room.chat_id, f"⏰ Hết giờ! {cur.first_name} mất lượt (còn {room.guess_left[cur.id]}).")
        if room.guess_left[cur.id] <= 0:
            await context.bot.send_message(room.chat_id, "🚫 Hết lượt – bị loại.")
            room.players = [u for u in room.players if u.id != cur.id]
            if not room.players:
                await context.bot.send_message(room.chat_id, "Hết người chơi. Kết thúc ván.")
                gc(context.chat_data)["room"] = None
                return
            room.turn_idx %= len(room.players)
        else:
            room.turn_idx = (room.turn_idx + 1) % len(room.players)
        await announce_next_turn_doan(context, room)

# -----------------------------
# /addqa (thêm câu hỏi đoán chữ) — chỉ dùng khi cần
# cú pháp: /addqa CÂU HỎI || ĐÁP ÁN || gợi ý1|gợi ý2
# -----------------------------
ADMIN_USERS = set(u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip())

async def addqa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_USERS and (user.username or "").lower() not in ADMIN_USERS:
        await update.message.reply_text("Bạn không có quyền dùng lệnh này.")
        return
    text = update.message.text.partition(" ")[2]
    parts = [p.strip() for p in text.split("||")]
    if len(parts) < 2:
        await update.message.reply_text("Sai cú pháp. Ví dụ:\n/addqa Mẹ đi chợ || Bán cá || mẹ|chợ|cá")
        return
    hints = []
    if len(parts) >= 3 and parts[2]:
        hints = [h.strip() for h in parts[2].split("|") if h.strip()]
    item = {"question": parts[0], "answer": parts[1], "hints": hints}
    await append_guess_item(item)
    await update.message.reply_text("✅ Đã lưu vào gist.")

# -----------------------------
# /start webhook server
# -----------------------------
def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # pre-load dict
    app.job_queue.run_once(lambda c: asyncio.create_task(DICT.load()), 0)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(on_doi_new, pattern="^doi_new$"))
    app.add_handler(CallbackQueryHandler(on_doan_new, pattern="^doan_new$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_new_join(u, c, "doi"), pattern="^doi_join$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_new_join(u, c, "doan"), pattern="^doan_join$"))
    app.add_handler(CommandHandler("join", on_join_cmd))
    app.add_handler(CommandHandler("addqa", addqa))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # chạy webhook server của PTB (aiohttp) – Render sẽ detect cổng $PORT
    port = int(os.environ.get("PORT", "8000"))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,                       # path bảo mật
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}", # Telegram sẽ gọi vào đây
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
