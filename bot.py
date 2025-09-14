# bot.py
import os
import json
import random
import asyncio
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, filters
)

# ==================== ENV ====================
BOT_TOKEN         = os.environ["BOT_TOKEN"]
BASE_URL          = os.environ.get("BASE_URL", "").rstrip("/")  # https://wordgame-bot.onrender.com
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "secret123")
SECRET_PATH       = os.environ.get("SECRET_PATH", "hook")

# Gist (chung 1 gist, 2 tệp khác nhau)
GIST_TOKEN        = os.environ.get("GIST_TOKEN", "")
GIST_ID           = os.environ.get("GIST_ID", "")
GIST_DICT_FILE    = os.environ.get("GIST_DICT_FILE", "dict_offline.txt")
GIST_QUIZ_FILE    = os.environ.get("GIST_QUIZ_FILE", "guess_clue_bank.json")

# ==================== STATE ====================
# Lưu state đơn giản trong RAM theo chat_id
ROOM: Dict[int, Dict] = {}

# Cà khịa (random 15 câu) – dùng chung cho cả 2 game
TRASH_TALK = [
    "Sai rồi! Đoán hên xui thế à? 😜",
    "Ơ kìa… gần đúng bằng 0! 🤭",
    "Chưa chạm vạch xuất phát luôn đó 🤣",
    "Đoán nữa là server tan chảy đó nha 😏",
    "Bạn ơi bớt liều, thêm xíu não! 🧠",
    "Quá xa chân trời! 🏜️",
    "Đúng… trong vũ trụ song song 🤡",
    "Cụm này không qua nổi vòng gửi xe 🤐",
    "Lại trượt vỏ chuối rồi! 🍌",
    "Chủ tịch gọi bảo thôi đừng đoán! 📵",
    "Đoán vậy là xúc phạm đáp án ghê á 😆",
    "Không khí đang mát, đừng đốt não nữa! 🔥",
    "Thêm tí muối cho mặn mà đi nào 🧂",
    "Thôi xong, bay màu! 🫥",
    "Chưa đúng nhưng có cố gắng… xíu xiu 😅"
]

# ==================== GIST HELPERS ====================
GITHUB_API = "https://api.github.com"

async def _gist_get_file(session: httpx.AsyncClient, filename: str) -> str:
    if not (GIST_TOKEN and GIST_ID):
        return ""
    headers = {"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
    r = await session.get(f"{GITHUB_API}/gists/{GIST_ID}", headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    files = data.get("files", {})
    if filename in files and files[filename].get("content") is not None:
        return files[filename]["content"]
    return ""

async def _gist_put_file(session: httpx.AsyncClient, filename: str, content: str) -> None:
    if not (GIST_TOKEN and GIST_ID):
        return
    headers = {"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {"files": {filename: {"content": content}}}
    r = await session.patch(f"{GITHUB_API}/gists/{GIST_ID}", headers=headers, json=payload, timeout=20)
    r.raise_for_status()

# Cache dict đã xác thực để lần sau khỏi lên mạng
async def dict_cache_has(phrase: str) -> bool:
    async with httpx.AsyncClient() as s:
        raw = await _gist_get_file(s, GIST_DICT_FILE)
        try:
            arr = json.loads(raw) if raw else []
        except Exception:
            arr = []
        return phrase.strip().lower() in {x.strip().lower() for x in arr}

async def dict_cache_add(phrase: str) -> None:
    async with httpx.AsyncClient() as s:
        raw = await _gist_get_file(s, GIST_DICT_FILE)
        try:
            arr = json.loads(raw) if raw else []
        except Exception:
            arr = []
        if phrase not in arr:
            arr.append(phrase)
            await _gist_put_file(s, GIST_DICT_FILE, json.dumps(arr, ensure_ascii=False, indent=2))

# Quiz bank helpers (đoán chữ)
async def quiz_bank_load() -> List[dict]:
    async with httpx.AsyncClient() as s:
        raw = await _gist_get_file(s, GIST_QUIZ_FILE)
        try:
            arr = json.loads(raw) if raw else []
        except Exception:
            arr = []
        return arr

async def quiz_bank_add(question: str, answer: str, hints: Optional[List[str]] = None) -> None:
    hints = hints or []
    async with httpx.AsyncClient() as s:
        raw = await _gist_get_file(s, GIST_QUIZ_FILE)
        try:
            arr = json.loads(raw) if raw else []
        except Exception:
            arr = []
        item = {"id": (max([x.get("id", 0) for x in arr]) + 1 if arr else 1),
                "question": question, "answer": answer, "hints": hints}
        arr.append(item)
        await _gist_put_file(s, GIST_QUIZ_FILE, json.dumps(arr, ensure_ascii=False, indent=2))

# ==================== SOHA CHECKER ====================
async def soha_valid(phrase: str) -> bool:
    """Heuristic: nếu trang trả về nội dung có box kết quả => coi là hợp lệ.
    Endpoint: http://tratu.soha.vn/dict/vn_vn/<cụm>
    """
    slug = phrase.strip().replace(" ", "%20")
    url = f"http://tratu.soha.vn/dict/vn_vn/{slug}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as s:
            r = await s.get(url)
            if r.status_code != 200:
                return False
            html = r.text.lower()
            # nếu có cụm “không tìm thấy” => sai
            if "không tìm thấy" in html or "khong tim thay" in html:
                return False
            # nếu có div trang từ điển (heuristic thô)
            soup = BeautifulSoup(r.text, "html.parser")
            if soup.find("div", id="content-5") or soup.find("div", class_="phantrang"):
                return True
            # fallback: nếu tiêu đề có cụm cần tìm
            title = (soup.title.get_text() if soup.title else "").lower()
            return phrase.strip().lower() in title
    except Exception:
        return False

async def phrase_is_valid(phrase: str) -> bool:
    if await dict_cache_has(phrase):
        return True
    ok = await soha_valid(phrase)
    if ok:
        await dict_cache_add(phrase)
    return ok

# ==================== GAME LOGIC ====================
def ensure_room(chat_id: int) -> Dict:
    if chat_id not in ROOM:
        ROOM[chat_id] = {"mode": None, "deadline": None, "turns": {}, "last_word": None, "quiz": None}
    return ROOM[chat_id]

def menu_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="mode:chain"),
            InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="mode:guess"),
        ]
    ])

RULES_CHAIN = (
    "🎮 *Game Đối Chữ*\n"
    "• Mặc định đếm 60 giây từ khi bắt đầu.\n"
    "• Gửi *cụm 2 từ có nghĩa* (VD: 'hoa mai'). Lượt sau phải bắt đầu bằng *từ cuối* của cụm trước.\n"
    "• Nếu chỉ 1 người, bạn sẽ đấu với BOT.\n"
    "• Từ hợp lệ được kiểm tra trên tratu.soha.vn và lưu cache vào Gist để lần sau tra nhanh."
)

RULES_GUESS = (
    "🧩 *Game Đoán Chữ*\n"
    "• Mặc định đếm 60 giây từ khi bắt đầu.\n"
    "• Mỗi người có *3 lượt đoán*, thay phiên nhau. Ai hết lượt trước sẽ bị loại.\n"
    "• Câu hỏi rút ngẫu nhiên từ Gist `guess_clue_bank.json`. Có thể thêm câu hỏi mới bằng lệnh /addquiz."
)

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Chọn trò nhé:", reply_markup=menu_kbd()
    )

async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    room = ensure_room(chat_id)

    if q.data == "mode:chain":
        room.update({"mode": "chain", "last_word": None, "deadline": None, "turns": {}})
        await q.edit_message_text(RULES_CHAIN, parse_mode="Markdown")
        await q.message.reply_text("Gõ /join để tham gia, rồi /begin để bắt đầu.")
    else:
        room.update({"mode": "guess", "quiz": None, "turns": {}, "deadline": None})
        await q.edit_message_text(RULES_GUESS, parse_mode="Markdown")
        await q.message.reply_text("Gõ /join để tham gia, rồi /begin để bắt đầu.")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room = ensure_room(chat_id)
    user = update.effective_user
    room["turns"].setdefault(user.id, {"name": user.full_name, "lives": 3})
    await update.message.reply_text(f"✅ {user.full_name} đã tham gia!")

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room = ensure_room(chat_id)
    if not room.get("mode"):
        await update.message.reply_text("Chưa chọn trò. Gõ /start để chọn nhé.")
        return
    room["deadline"] = asyncio.get_running_loop().time() + 60
    if room["mode"] == "chain":
        await update.message.reply_text("Bắt đầu Đối Chữ! Gửi *cụm 2 từ có nghĩa*.", parse_mode="Markdown")
    else:
        # bốc quiz
        bank = await quiz_bank_load()
        if not bank:
            await update.message.reply_text("Chưa có câu hỏi nào trong Gist. Dùng /addquiz để thêm.")
            return
        room["quiz"] = random.choice(bank)
        await update.message.reply_text(f"Câu hỏi: {room['quiz']['question']}")

async def cmd_addquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # cú pháp: /addquiz <câu hỏi> || <đáp án> || gợi ý1 || gợi ý2 ...
    try:
        raw = update.message.text.split(" ", 1)[1]
        parts = [p.strip() for p in raw.split("||")]
        question, answer = parts[0], parts[1]
        hints = [h for h in parts[2:] if h]
    except Exception:
        await update.message.reply_text("Cú pháp: /addquiz CÂU HỎI || ĐÁP ÁN || gợi ý1 || gợi ý2 ...")
        return
    await quiz_bank_add(question, answer, hints)
    await update.message.reply_text("✅ Đã lưu vào Gist (vĩnh viễn).")

def time_left(room: Dict) -> int:
    if not room.get("deadline"):
        return 0
    remain = int(room["deadline"] - asyncio.get_running_loop().time())
    return max(remain, 0)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room = ensure_room(chat_id)
    if not room.get("mode"):
        return

    if time_left(room) == 0:
        await update.message.reply_text("⏰ Hết giờ! /begin để chơi ván mới.")
        room["deadline"] = None
        return

    text = update.message.text.strip()
    user = update.effective_user
    room["turns"].setdefault(user.id, {"name": user.full_name, "lives": 3})

    if room["mode"] == "chain":
        # phải là 2 từ
        parts = [p for p in text.split() if p]
        if len(parts) != 2:
            await update.message.reply_text(random.choice(TRASH_TALK))
            return
        # nếu có last_word thì phải trùng từ đầu
        if room["last_word"] and parts[0].lower() != room["last_word"].lower():
            await update.message.reply_text("❌ Sai luật: phải bắt đầu bằng *từ cuối* của cụm trước.", parse_mode="Markdown")
            return
        # kiểm tra soha + cache gist
        if await phrase_is_valid(text):
            await update.message.reply_text("✅ Hợp lệ!")
            room["last_word"] = parts[-1]
            room["deadline"] = asyncio.get_running_loop().time() + 60  # reset 60s
        else:
            await update.message.reply_text(f"❌ Cụm không có nghĩa. {random.choice(TRASH_TALK)}")

    else:
        # guess
        quiz = room.get("quiz")
        if not quiz:
            await update.message.reply_text("Chưa có câu hỏi, gõ /begin trước nhé.")
            return
        ans_norm = quiz["answer"].strip().lower()
        if text.strip().lower() == ans_norm:
            await update.message.reply_text("🎉 Chính xác! /begin để ra câu khác.")
            room["quiz"] = None
            room["deadline"] = None
            return
        # trừ lượt
        lives = room["turns"][user.id]["lives"]
        lives -= 1
        room["turns"][user.id]["lives"] = lives
        if lives <= 0:
            await update.message.reply_text(f"🪦 {user.full_name} đã hết lượt!")
        else:
            await update.message.reply_text(f"❌ Sai! {random.choice(TRASH_TALK)} (còn {lives}/3 lượt)")

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_CHAIN + "\n\n" + RULES_GUESS, parse_mode="Markdown")

# ==================== LIFECYCLE ====================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("addquiz", cmd_addquiz))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def initialize(app: Application):
    """Set webhook (nếu có BASE_URL)."""
    if BASE_URL:
        url = f"{BASE_URL}/{SECRET_PATH}/{WEBHOOK_SECRET}"
        await app.bot.set_webhook(url=url, allowed_updates=Update.ALL_TYPES)
    else:
        # fallback chạy polling local
        asyncio.create_task(app.run_polling(close_loop=False))

async def stop(app: Application):
    try:
        await app.bot.delete_webhook()
    except Exception:
        pass
