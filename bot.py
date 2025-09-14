# bot.py
import os
import json
import random
import asyncio
import time
import urllib.parse
from typing import Dict, List, Optional, Any, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat, Message, User
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ================== ENV ==================
BOT_TOKEN       = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET  = os.environ["WEBHOOK_SECRET"]   # ví dụ: a1b2c3_webhook
BASE_URL        = os.environ["BASE_URL"].rstrip("/")  # https://wordgame-bot.onrender.com

# Gist: chung 1 gist chứa 2 file: dict_offline.txt và guess_clue_bank.json
GIST_ID         = os.environ["GIST_ID"]          # ví dụ: 212301c00d2b00247ffc786f921dc29f
GIST_TOKEN      = os.environ["GIST_TOKEN"]       # token classic có scope gist
GIST_DICT_FILE  = os.environ.get("GIST_DICT_FILE", "dict_offline.txt")
GIST_CLUE_FILE  = os.environ.get("GIST_CLUE_FILE", "guess_clue_bank.json")

# ================== FASTAPI + PTB ==================
def build_app() -> FastAPI:
    app = FastAPI()

    tg_app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # ── Handlers ─────────────────────────────────────────────────────
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_main_menu, pattern="^menu:"))
    tg_app.add_handler(CommandHandler("newgame", cmd_newgame))       # mở sảnh game đối chữ
    tg_app.add_handler(CommandHandler("joindc", cmd_join_dc))        # join đối chữ
    tg_app.add_handler(CommandHandler("begin", cmd_begin_dc))        # cưỡng chế bắt đầu đối chữ (nếu cần)

    tg_app.add_handler(CommandHandler("newguess", cmd_newguess))     # mở sảnh đoán chữ
    tg_app.add_handler(CommandHandler("joinguess", cmd_join_guess))  # join đoán chữ
    tg_app.add_handler(CommandHandler("addclue", cmd_add_clue))      # thêm câu hỏi (admin tuỳ chọn)

    # tin nhắn trong 2 game
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    app.state.tg_app = tg_app

    # Webhook endpoint
    @app.post(f"/webhook/{WEBHOOK_SECRET}")
    async def webhook(update_json: dict):
        update = Update.de_json(update_json, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}

    @app.get("/")
    async def root():
        return {"ok": "wordgame running"}

    return app


async def initialize(app: FastAPI):
    tg_app: Application = app.state.tg_app
    await tg_app.bot.set_webhook(f"{BASE_URL}/webhook/{WEBHOOK_SECRET}")


async def stop(app: FastAPI):
    tg_app: Application = app.state.tg_app
    try:
        await tg_app.bot.delete_webhook()
    except Exception:
        pass
    await tg_app.shutdown()

# ================== TIỆN ÍCH ==================
KICK_LINES = [
    "Ủa? Câu đó nghe sai sai á.", "Thôi đừng liều nữa bạn hiền ơi.",
    "Cà khịa tí: câu đó không ổn đâu nha!", "Sai béng rồi, tỉnh táo lên!",
    "Bậy quá xá bậy!", "Còn hơn thua gì nữa, sai rồi!", "Không qua mắt được tui đâu!",
    "Rớt đài :))", "Coi bộ hên xui quá ta!", "Thử lại đi nè.",
    "Ôi trời ơi…", "Không phải vậy đâu!", "Trật lất!",
    "Sai nhẹ mà đau lòng :))", "Về ôn bài nhen!"
]

def now_ts() -> float: return time.time()

# =========== GIST ===========
GIST_API = "https://api.github.com"

async def gist_get_file(session: httpx.AsyncClient, filename: str) -> str:
    url = f"{GIST_API}/gists/{GIST_ID}"
    r = await session.get(url, headers={"Authorization": f"token {GIST_TOKEN}"})
    r.raise_for_status()
    data = r.json()
    files = data.get("files", {})
    if filename in files and files[filename].get("content") is not None:
        return files[filename]["content"]
    # nếu file chưa có -> trả rỗng tương ứng
    return "[]" if filename.endswith(".json") else "[]"

async def gist_update_file(session: httpx.AsyncClient, filename: str, content: str) -> None:
    url = f"{GIST_API}/gists/{GIST_ID}"
    payload = {"files": {filename: {"content": content}}}
    r = await session.patch(url, json=payload, headers={
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    })
    r.raise_for_status()

# =========== TỪ ĐIỂN OFFLINE + ONLINE ===========
async def load_offline_set(session: httpx.AsyncClient) -> set:
    raw = await gist_get_file(session, GIST_DICT_FILE)
    try:
        # cho phép lưu mảng string hoặc JSON lines
        data = json.loads(raw)
        if isinstance(data, list): return set(map(lambda s: s.strip(), data))
    except Exception:
        pass
    # fallback: mỗi dòng 1 cụm
    return set([s.strip() for s in raw.splitlines() if s.strip()])

async def save_offline_set(session: httpx.AsyncClient, s: set) -> None:
    content = json.dumps(sorted(s), ensure_ascii=False, indent=0)
    await gist_update_file(session, GIST_DICT_FILE, content)

async def online_lookup_tratu(term: str) -> bool:
    # tra trực tiếp trên tratu.soha.vn (đơn giản: có trang kết quả hợp lệ)
    url = f"http://tratu.soha.vn/dict/vn_vn/{urllib.parse.quote(term)}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return False
        html = r.text.lower()
        # Một số cụm từ khóa "không tìm thấy" thường gặp:
        bad_markers = [
            "không tìm thấy", "không có kết quả", "chưa có mục từ", "không tồn tại"
        ]
        if any(m in html for m in bad_markers):
            return False
        # nếu trang có khối nghĩa (thường có thẻ id 'content' / 'result'), bắt heuristics nhẹ
        soup = BeautifulSoup(r.text, "html.parser")
        # tìm thử các khối định nghĩa
        blocks = soup.select("#content, .content, .result, .itd, .tdw, .td_box")
        text = " ".join([b.get_text(" ", strip=True) for b in blocks]).strip()
        return len(text) >= 10  # có nội dung “đủ dài” xem như có nghĩa

async def is_valid_phrase(term: str) -> bool:
    term = term.strip()
    if not term or " " not in term:  # cần cụm 2 từ trở lên
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        offline = await load_offline_set(client)
        if term in offline:
            return True
        ok = await online_lookup_tratu(term)
        if ok:
            offline.add(term)
            await save_offline_set(client, offline)
        return ok

# =========== CLUE BANK ===========
async def load_clue_bank(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    raw = await gist_get_file(session, GIST_CLUE_FILE)
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    return []

async def save_clue_bank(session: httpx.AsyncClient, arr: List[Dict[str, Any]]) -> None:
    await gist_update_file(session, GIST_CLUE_FILE, json.dumps(arr, ensure_ascii=False, indent=2))

# ================== QUẢN LÝ PHÒNG/STATE ==================
class WordChainRoom:
    def __init__(self, chat_id: int, host_id: int):
        self.chat_id = chat_id
        self.host_id = host_id
        self.players: List[int] = []
        self.started = False
        self.current_phrase: Optional[str] = None
        self.turn_index = 0
        self.turn_deadline = 0.0
        self.mode_bot_play = False  # 1 người -> chơi với BOT
        self.alive: Dict[int, bool] = {}  # loại khi sai/timeout

    def alive_players(self) -> List[int]:
        return [uid for uid in self.players if self.alive.get(uid, True)]

    def current_player(self) -> Optional[int]:
        alive = self.alive_players()
        if not alive: return None
        return alive[self.turn_index % len(alive)]

class GuessRoom:
    def __init__(self, chat_id: int, host_id: int):
        self.chat_id = chat_id
        self.host_id = host_id
        self.players: List[int] = []
        self.started = False
        self.turn_index = 0
        self.turn_deadline = 0.0
        self.guess_left: Dict[int, int] = {}  # 3 mỗi người
        self.question: Optional[Dict[str, Any]] = None

    def alive_players(self) -> List[int]:
        return [uid for uid in self.players if self.guess_left.get(uid, 0) > 0]

    def current_player(self) -> Optional[int]:
        alive = self.alive_players()
        if not alive: return None
        return alive[self.turn_index % len(alive)]

ROOM_DC: Dict[int, WordChainRoom] = {}      # chat_id -> room
ROOM_GUESS: Dict[int, GuessRoom] = {}       # chat_id -> room

# ================== UI ==================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="menu:dc"),
        InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="menu:guess"),
    ]])

def dc_lobby_text() -> str:
    return ("🎮 *Đối Chữ* \n"
            "Luật: đối *cụm 2 từ có nghĩa*. Lượt sau phải bắt đầu bằng *từ cuối* của cụm trước.\n"
            "⏱ Mỗi lượt 30s. Sai hoặc hết giờ sẽ *bị loại*.\n"
            "▫️ /newgame – mở sảnh (60s).\n"
            "▫️ /joindc – tham gia.\n"
            "▫️ /begin – bắt đầu ngay (nếu cần).\n"
            "Một người → đấu với BOT. Từ hợp lệ được xác minh online & cache vào Gist.")

def guess_lobby_text() -> str:
    return ("🧩 *Đoán Chữ* \n"
            "Câu hỏi từ ca dao, thành ngữ… *mỗi người có 3 lượt đoán*. Hết lượt bị loại.\n"
            "⏱ Mỗi lượt 30s. \n"
            "▫️ /newguess – mở sảnh (60s)\n"
            "▫️ /joinguess – tham gia\n"
            "▫️ /addclue câu|đáp án|gợi ý1;gợi ý2 (để thêm bank – lưu Gist)")

# ================== HANDLERS ==================
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.effective_chat.send_message(
        "Chọn chế độ bạn muốn chơi nhen 👇",
        reply_markup=main_menu_kb()
    )

async def on_main_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    await q.answer()
    if q.data == "menu:dc":
        await q.message.reply_text(dc_lobby_text(), parse_mode="Markdown")
    elif q.data == "menu:guess":
        await q.message.reply_text(guess_lobby_text(), parse_mode="Markdown")

# ---------- ĐỐI CHỮ ----------
async def cmd_newgame(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    ROOM_DC[chat.id] = WordChainRoom(chat.id, upd.effective_user.id)
    ROOM_DC[chat.id].players = []
    ROOM_DC[chat.id].started = False
    ROOM_DC[chat.id].current_phrase = None
    ROOM_DC[chat.id].turn_index = 0
    ROOM_DC[chat.id].alive = {}
    await chat.send_message("🕹 Mở sảnh đối chữ! Gõ /joindc để tham gia. 🔔 Tự bắt đầu sau 60s nếu có người tham gia.")
    # đếm ngược 60s
    await asyncio.sleep(60)
    room = ROOM_DC.get(chat.id)
    if room and not room.started and room.players:
        await begin_dc(chat, ctx)

async def cmd_join_dc(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    room = ROOM_DC.get(chat.id)
    if not room:
        await chat.send_message("Chưa có sảnh. Gõ /newgame để mở sảnh.")
        return
    uid = upd.effective_user.id
    if uid not in room.players:
        room.players.append(uid)
        room.alive[uid] = True
        await chat.send_message(f"✅ {upd.effective_user.full_name} đã tham gia!")
    else:
        await chat.send_message("Bạn đã tham gia rồi nha.")

async def cmd_begin_dc(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    room = ROOM_DC.get(chat.id)
    if not room or room.started:
        return
    if not room.players:
        await chat.send_message("Chưa có người tham gia.")
        return
    await begin_dc(chat, ctx)

async def begin_dc(chat: Chat, ctx: ContextTypes.DEFAULT_TYPE):
    room = ROOM_DC.get(chat.id)
    if not room: return
    room.started = True
    if len(room.players) == 1:
        room.mode_bot_play = True
        await chat.send_message("👤 Chỉ 1 người → chơi với BOT.\n✨ Lượt đầu: gửi *cụm 2 từ có nghĩa* bất kỳ.", parse_mode="Markdown")
    else:
        room.mode_bot_play = False
        random.shuffle(room.players)
        first = room.current_player()
        await chat.send_message("👥 Nhiều người → BOT làm trọng tài.\n✨ Lượt đầu: gửi *cụm 2 từ có nghĩa* bất kỳ.", parse_mode="Markdown")
        await announce_turn(chat, ctx, first)

async def announce_turn(chat: Chat, ctx: ContextTypes.DEFAULT_TYPE, uid: Optional[int]):
    room = ROOM_DC.get(chat.id)
    if not room or uid is None: return
    room.turn_deadline = now_ts() + 30
    mention = f"[{uid}](tg://user?id={uid})"
    if room.current_phrase:
        last_word = room.current_phrase.split()[-1]
        await chat.send_message(
            f"⏳ Đến lượt {mention}. Gửi cụm 2 từ bắt đầu bằng: *{last_word}*",
            parse_mode="Markdown")
    else:
        await chat.send_message(
            f"⏳ Đến lượt {mention}. Gửi cụm 2 từ có nghĩa bắt kỳ.",
            parse_mode="Markdown")

# xử lý tin nhắn trong game đối chữ
async def handle_dc_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    chat = upd.effective_chat
    room = ROOM_DC.get(chat.id)
    if not room or not room.started:
        return

    uid = upd.effective_user.id

    # nếu đang nhiều người → phải đúng lượt
    if not room.mode_bot_play:
        cur = room.current_player()
        if uid != cur:
            return  # lờ tin nhắn ngoài lượt
    # kiểm soát thời gian
    if now_ts() > room.turn_deadline:
        await chat.send_message(f"⏰ Hết giờ! {random.choice(KICK_LINES)}")
        # loại người chơi này
        if room.mode_bot_play:
            await chat.send_message("BOT thắng! 👑")
            ROOM_DC.pop(chat.id, None)
            return
        else:
            room.alive[uid] = False
            if len(room.alive_players()) <= 1:
                await end_dc(chat)
                return
            room.turn_index += 1
            await announce_turn(chat, ctx, room.current_player())
            return

    phrase = text.strip()
    # kiểm tra rule “bắt đầu bằng từ cuối”
    if room.current_phrase:
        must = room.current_phrase.split()[-1].lower()
        if not phrase.lower().startswith(must + " "):
            await chat.send_message(f"❌ Sai nhịp (phải bắt đầu bằng **{must}**). {random.choice(KICK_LINES)}", parse_mode="Markdown")
            if room.mode_bot_play:
                await chat.send_message("BOT thắng! 👑")
                ROOM_DC.pop(chat.id, None)
                return
            room.alive[uid] = False
            if len(room.alive_players()) <= 1:
                await end_dc(chat); return
            room.turn_index += 1
            await announce_turn(chat, ctx, room.current_player())
            return

    # kiểm tra nghĩa (offline→online)
    ok = await is_valid_phrase(phrase)
    if not ok:
        await chat.send_message(f"❌ Cụm không có nghĩa (không tìm thấy). {random.choice(KICK_LINES)}")
        if room.mode_bot_play:
            await chat.send_message("BOT thắng! 👑")
            ROOM_DC.pop(chat.id, None); return
        room.alive[uid] = False
        if len(room.alive_players()) <= 1:
            await end_dc(chat); return
        room.turn_index += 1
        await announce_turn(chat, ctx, room.current_player())
        return

    # hợp lệ
    room.current_phrase = phrase
    await chat.send_message(f"✅ Hợp lệ: *{phrase}*", parse_mode="Markdown")

    if room.mode_bot_play:
        # BOT “đỡ” đơn giản: lấy từ cuối + chêm 1 cụm đã có sẵn trong cache nếu tìm được
        last = phrase.split()[-1].lower()
        # thử invent câu mới: "{last} quá" (cũng 2 từ) → nhưng phải có nghĩa, nên dùng fallback
        bot_try = f"{last} quá"
        if not await is_valid_phrase(bot_try):
            bot_try = f"{last} thật"
        if not await is_valid_phrase(bot_try):
            await chat.send_message("🤖 BOT chịu! Bạn thắng 👑")
            ROOM_DC.pop(chat.id, None); return
        await asyncio.sleep(1.2)
        await chat.send_message(f"🤖 BOT: {bot_try}")
        room.current_phrase = bot_try
        room.turn_deadline = now_ts() + 30
        return

    # nhiều người → chuyển lượt
    room.turn_index += 1
    await announce_turn(chat, ctx, room.current_player())

async def end_dc(chat: Chat):
    room = ROOM_DC.get(chat.id)
    if not room: return
    survivors = room.alive_players()
    if survivors:
        winner = survivors[0]
        await chat.send_message(f"🏁 Kết thúc! Người thắng: [{winner}](tg://user?id={winner}) 👑", parse_mode="Markdown")
    else:
        await chat.send_message("🏁 Kết thúc! Không còn ai sống sót 😅")
    ROOM_DC.pop(chat.id, None)

# ---------- ĐOÁN CHỮ ----------
async def cmd_newguess(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    ROOM_GUESS[chat.id] = GuessRoom(chat.id, upd.effective_user.id)
    await chat.send_message("🧩 Mở sảnh đoán chữ! Gõ /joinguess để tham gia. 🔔 Tự bắt đầu sau 60s nếu có người tham gia.")
    await asyncio.sleep(60)
    room = ROOM_GUESS.get(chat.id)
    if room and not room.started and room.players:
        await begin_guess(chat, ctx)

async def cmd_join_guess(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    room = ROOM_GUESS.get(chat.id)
    if not room:
        await chat.send_message("Chưa có sảnh. Gõ /newguess để mở sảnh.")
        return
    uid = upd.effective_user.id
    if uid not in room.players:
        room.players.append(uid)
        room.guess_left[uid] = 3
        await chat.send_message(f"✅ {upd.effective_user.full_name} đã tham gia!")
    else:
        await chat.send_message("Bạn đã tham gia rồi nha.")

async def begin_guess(chat: Chat, ctx: ContextTypes.DEFAULT_TYPE):
    room = ROOM_GUESS.get(chat.id)
    if not room: return
    # tải bank, random câu
    async with httpx.AsyncClient(timeout=10) as client:
        bank = await load_clue_bank(client)
    if not bank:
        await chat.send_message("Chưa có câu hỏi trong ngân hàng. Dùng /addclue để thêm nha.")
        ROOM_GUESS.pop(chat.id, None); return
    room.question = random.choice(bank)
    room.started = True
    random.shuffle(room.players)
    await chat.send_message(
        "✨ Bắt đầu *Đoán Chữ*!\n"
        f"❓ Câu hỏi: {room.question.get('question','(trống)')}\n"
        f"💡 Gợi ý: {', '.join(room.question.get('hints', [])[:2]) if room.question.get('hints') else '—'}\n"
        "Mỗi người *3 lượt đoán*, hết lượt bị loại.",
        parse_mode="Markdown")
    await announce_guess_turn(chat)

async def announce_guess_turn(chat: Chat):
    room = ROOM_GUESS.get(chat.id)
    if not room: return
    uid = room.current_player()
    if uid is None:
        await chat.send_message("🏁 Hết người đoán. Kết thúc!")
        ROOM_GUESS.pop(chat.id, None); return
    room.turn_deadline = now_ts() + 30
    await chat.send_message(f"🎯 Đến lượt [{uid}](tg://user?id={uid}) – bạn còn {room.guess_left.get(uid, 0)} lượt.", parse_mode="Markdown")

async def handle_guess_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    chat = upd.effective_chat
    room = ROOM_GUESS.get(chat.id)
    if not room or not room.started: return
    uid = upd.effective_user.id

    if uid != room.current_player():
        return
    if now_ts() > room.turn_deadline:
        await chat.send_message(f"⏰ Hết giờ! {random.choice(KICK_LINES)}")
        room.guess_left[uid] = max(0, room.guess_left.get(uid, 0) - 1)
        if not room.alive_players():
            await chat.send_message("🏁 Hết người đoán. Kết thúc!")
            ROOM_GUESS.pop(chat.id, None); return
        room.turn_index += 1
        await announce_guess_turn(chat)
        return

    answer = (room.question.get("answer", "") if room.question else "").strip().lower()
    if answer and text.strip().lower() == answer:
        await chat.send_message(f"✅ Chính xác! [{uid}](tg://user?id={uid}) thắng 👑", parse_mode="Markdown")
        ROOM_GUESS.pop(chat.id, None); return

    # sai → trừ lượt
    room.guess_left[uid] = max(0, room.guess_left.get(uid, 0) - 1)
    msg = f"❌ Sai rồi. {random.choice(KICK_LINES)} – Bạn còn {room.guess_left[uid]} lượt."
    await chat.send_message(msg)
    if not room.alive_players():
        await chat.send_message(f"🏁 Hết người đoán. Đáp án: *{room.question.get('answer','?')}*", parse_mode="Markdown")
        ROOM_GUESS.pop(chat.id, None); return
    room.turn_index += 1
    await announce_guess_turn(chat)

# thêm câu hỏi: /addclue câu|đáp án|gợi ý1;gợi ý2
async def cmd_add_clue(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat
    args = (upd.message.text or "").split(" ", 1)
    if len(args) < 2:
        await chat.send_message("Cách dùng: /addclue câu|đáp án|gợi ý1;gợi ý2")
        return
    body = args[1]
    try:
        q, ans, hints = body.split("|", 2)
    except ValueError:
        await chat.send_message("Định dạng sai. Dùng: /addclue câu|đáp án|gợi ý1;gợi ý2")
        return
    hints_list = [h.strip() for h in hints.split(";") if h.strip()]
    new_item = {"question": q.strip(), "answer": ans.strip(), "hints": hints_list}

    async with httpx.AsyncClient(timeout=10) as client:
        bank = await load_clue_bank(client)
        bank.append(new_item)
        await save_clue_bank(client, bank)

    await chat.send_message("✅ Đã lưu câu hỏi vào Gist (vĩnh viễn).")

# ---------- Router TEXT ----------
async def on_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = upd.message.text or ""
    chat_id = upd.effective_chat.id
    if chat_id in ROOM_DC and ROOM_DC[chat_id].started:
        await handle_dc_text(upd, ctx, text)
    elif chat_id in ROOM_GUESS and ROOM_GUESS[chat_id].started:
        await handle_guess_text(upd, ctx, text)
    else:
        # ngoài game: bỏ qua
        pass
