# -*- coding: utf-8 -*-
"""
webhook.py — FastAPI + python-telegram-bot v20.8
Hai game: Đối Chữ (word check qua tratu.soha.vn + cache Gist)
         Đoán Chữ (random câu hỏi từ Gist)

Thiết kế để "không chết app":
- Pin version libs ổn định
- Không dùng lxml; chỉ BeautifulSoup(html.parser)
- Try/except bọc toàn bộ I/O mạng
- Nếu thiếu env -> log cảnh báo, vẫn trả 200 cho webhook để Telegram không retry liên tục
"""

import os
import json
import time
import asyncio
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, status

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ===================== ENV =====================
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
BASE_URL         = os.environ.get("BASE_URL", "")  # ví dụ https://wordgame-bot.onrender.com
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "hook")

GIST_TOKEN       = os.environ.get("GIST_TOKEN", "")
GIST_ID          = os.environ.get("GIST_ID", "")
GIST_DICT_FILE   = os.environ.get("GIST_DICT_FILE", "dict_offline.txt")
GIST_CLUE_FILE   = os.environ.get("GIST_CLUE_FILE", "guess_clue_bank.json")

LOBBY_TIMEOUT    = int(os.environ.get("LOBBY_TIMEOUT", "60"))

# ===================== GLOBAL STATE =====================
app = FastAPI()
application: Optional[Application] = None

# cache Gist tại memory (để giảm request)
DICT_CACHE: set = set()    # các từ hợp lệ đã xác minh
CLUE_BANK: List[Dict] = [] # các câu hỏi đoán chữ

# Trạng thái game theo chat
GAMES: Dict[int, Dict] = {}  # chat_id -> state dict

# Cà khịa
TAUNTS = [
    "Sai rồi nha! 🤭", "Trượt mất rồi 😝", "Không đúng nha bạn ơi!",
    "Gần đúng… nhưng không phải 😅", "Sai bét 🤣", "Đoán hên xui quá ta!",
    "Hụt rồi nha 😜", "Lệch nhẹ thôi! 😬", "Thử lại lần nữa coi 😉",
    "Trùm đoán sai là đây 😂", "Không phải đáp án đâu!", "Thêm xíu muối nữa nè 🧂",
    "Vẫn chưa đúng 😶‍🌫️", "Ơ kìa… hông phải!", "Cố lên bạn ơi 💪"
]

# ===================== UTILS: Gist =====================
GITHUB_API = "https://api.github.com"

async def gist_get_file_raw_url(client: httpx.AsyncClient, gist_id: str, filename: str) -> Optional[str]:
    try:
        r = await client.get(f"{GITHUB_API}/gists/{gist_id}",
                             headers={"Authorization": f"token {GIST_TOKEN}"} if GIST_TOKEN else {})
        if r.status_code == 200:
            data = r.json()
            files = data.get("files", {})
            f = files.get(filename)
            if f and f.get("raw_url"):
                return f["raw_url"]
    except Exception:
        pass
    return None

async def gist_read_text(filename: str) -> str:
    if not GIST_ID:
        return ""
    async with httpx.AsyncClient(timeout=15) as client:
        raw = await gist_get_file_raw_url(client, GIST_ID, filename)
        if not raw:
            return ""
        try:
            r = await client.get(raw)
            if r.status_code == 200:
                return r.text
        except Exception:
            return ""
    return ""

async def gist_write_text(filename: str, content: str) -> bool:
    if not (GIST_ID and GIST_TOKEN):
        return False
    payload = {"files": {filename: {"content": content}}}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.patch(f"{GITHUB_API}/gists/{GIST_ID}",
                                   headers={
                                       "Authorization": f"token {GIST_TOKEN}",
                                       "Accept": "application/vnd.github+json",
                                   },
                                   json=payload)
            return r.status_code in (200, 201)
    except Exception:
        return False

# ===================== UTILS: từ điển soha =====================
async def soha_is_valid_word(word: str) -> bool:
    """Tra nhanh tratu.soha.vn; nếu trang có kết quả -> coi là hợp lệ.
       Dùng html.parser để tránh lxml.
    """
    try:
        url = f"http://tratu.soha.vn/dict/vn_vn/{word}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return False
            html = r.text
            # heuristics: có vùng nội dung định nghĩa
            soup = BeautifulSoup(html, "html.parser")
            # Thường có id="content-5" hoặc class chứa "word"
            if soup.find("div", id=lambda x: x and "content" in x) or soup.find("h2"):
                txt = soup.get_text(" ", strip=True)
                # nếu thấy cụm “Kết quả”, “Từ điển”, hay có nhiều chữ -> chấp nhận
                return any(k in txt for k in ["Kết quả", "Từ điển", "Định nghĩa"]) or len(txt) > 200
    except Exception:
        return False
    return False

# ===================== LOAD CACHED DATA =====================
async def load_dict_cache():
    DICT_CACHE.clear()
    txt = await gist_read_text(GIST_DICT_FILE)
    try:
        arr = json.loads(txt) if txt.strip() else []
        if isinstance(arr, list):
            for w in arr:
                if isinstance(w, str):
                    DICT_CACHE.add(w.lower())
    except Exception:
        pass

async def save_dict_cache():
    try:
        data = json.dumps(sorted(list(DICT_CACHE)), ensure_ascii=False, indent=2)
        await gist_write_text(GIST_DICT_FILE, data)
    except Exception:
        pass

async def load_clue_bank():
    CLUE_BANK.clear()
    txt = await gist_read_text(GIST_CLUE_FILE)
    try:
        arr = json.loads(txt) if txt.strip() else []
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and "question" in item and "answer" in item:
                    CLUE_BANK.append({
                        "id": item.get("id"),
                        "question": item["question"],
                        "answer": str(item["answer"]).strip()
                    })
    except Exception:
        pass

# ===================== GAME HELPERS =====================
def start_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="game:doi")],
        [InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="game:doan")],
    ])

def join_kb(game: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tham gia", callback_data=f"join:{game}")],
        [InlineKeyboardButton("🚀 Bắt đầu ngay", callback_data=f"start:{game}")]
    ])

def random_taunt() -> str:
    import random
    return random.choice(TAUNTS)

# ===================== HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Chọn game nhé!", reply_markup=start_menu_kb()
    )

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "game:doi":
        # khởi lobby Đối Chữ
        GAMES[chat_id] = {
            "type": "doi",
            "players": set(),
            "started": False,
            "deadline": time.time() + LOBBY_TIMEOUT,
            "turn": None,
            "last_word": None
        }
        txt = (
            "🧩 *ĐỐI CHỮ*\n"
            "• Mỗi lượt gửi *1 từ có nghĩa* (tiếng Việt có dấu).\n"
            "• Bot sẽ tra từ ở tratu.soha.vn và lưu cache Gist cho lần sau.\n"
            f"• Lobby sẽ tự start sau {LOBBY_TIMEOUT}s kể từ khi tạo.\n"
            "Ấn *Tham gia* để vào bàn, hoặc *Bắt đầu ngay* để chơi luôn."
        )
        await query.message.reply_markdown_v2(txt, reply_markup=join_kb("doi"))

    elif data == "game:doan":
        GAMES[chat_id] = {
            "type": "doan",
            "players": set(),
            "started": False,
            "deadline": time.time() + LOBBY_TIMEOUT,
            "current": None,     # {q, a}
            "lives": {},         # user_id -> lượt còn (3)
            "order": [],         # thứ tự chơi
            "turn_idx": 0
        }
        txt = (
            "🧠 *ĐOÁN CHỮ*\n"
            "• Mỗi người có *3 lượt đoán* luân phiên.\n"
            "• Nếu hết lượt mà chưa đúng thì *thua cả bàn*.\n"
            "• Kể cả chỉ 1 người cũng chơi được.\n"
            f"• Lobby tự start sau {LOBBY_TIMEOUT}s.\n"
            "Ấn *Tham gia* để vào bàn, hoặc *Bắt đầu ngay* để chơi luôn."
        )
        await query.message.reply_markdown_v2(txt, reply_markup=join_kb("doan"))

async def on_join_or_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user = query.from_user

    st = GAMES.get(chat_id)
    if not st:
        await query.message.reply_text("Lobby đã hết hạn, /start lại nhé.")
        return

    action, game = data.split(":", 1)

    if action == "join":
        st["players"].add(user.id)
        await query.message.reply_text(f"✅ {user.first_name} đã tham gia.")

    if action == "start":
        st["deadline"] = time.time()  # ép start ngay

    # nếu hết thời gian hoặc host bấm start
    if not st["started"] and time.time() >= st["deadline"]:
        st["started"] = True
        if st["type"] == "doi":
            await start_doi_chu(chat_id, context)
        else:
            await start_doan_chu(chat_id, context)

async def periodic_lobby_checker(context: ContextTypes.DEFAULT_TYPE):
    """Job chạy mỗi 5s để tự start lobby quá hạn."""
    now = time.time()
    to_start: List[Tuple[int, str]] = []
    for chat_id, st in list(GAMES.items()):
        if not st["started"] and now >= st.get("deadline", 0):
            to_start.append((chat_id, st["type"]))
    for chat_id, typ in to_start:
        GAMES[chat_id]["started"] = True
        if typ == "doi":
            await start_doi_chu(chat_id, context)
        else:
            await start_doan_chu(chat_id, context)

# -------- ĐỐI CHỮ --------
async def start_doi_chu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = GAMES.get(chat_id)
    if not st:
        return
    if not st["players"]:
        # nếu không ai join -> cho user gửi cũng chơi được
        await context.bot.send_message(chat_id, "Không ai tham gia, ai nhắn *trước* sẽ đấu với BOT nhé.", parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id, "Bắt đầu *Đối Chữ*! Gửi 1 từ tiếng Việt có nghĩa.", parse_mode="Markdown")

async def handle_text_doi_chu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = GAMES.get(chat_id)
    if not st or st.get("type") != "doi" or not st.get("started"):
        return

    word = update.effective_message.text.strip().lower()
    if not word or " " in word:
        await update.effective_message.reply_text(random_taunt())
        return

    # kiểm tra cache trước
    ok = word in DICT_CACHE
    if not ok:
        # tra soha
        ok = await soha_is_valid_word(word)
        if ok:
            DICT_CACHE.add(word)
            await save_dict_cache()

    if ok:
        st["last_word"] = word
        await update.effective_message.reply_text(f"✅ Hợp lệ: *{word}*", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(f"❌ {random_taunt()} — *{word}* không thấy trong từ điển.", parse_mode="Markdown")

# -------- ĐOÁN CHỮ --------
async def start_doan_chu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = GAMES.get(chat_id)
    if not st:
        return

    # chọn câu hỏi
    import random
    if not CLUE_BANK:
        await context.bot.send_message(chat_id, "Ngân hàng câu hỏi trống. Hãy thêm dữ liệu vào Gist.")
        return

    q = random.choice(CLUE_BANK)
    st["current"] = q
    # set lượt
    if not st["players"]:
        st["players"] = set([0])   # 0 đại diện “khách vãng lai”
        st["order"] = [0]
        st["lives"] = {0: 3}
    else:
        st["order"] = list(st["players"])
        random.shuffle(st["order"])
        st["lives"] = {uid: 3 for uid in st["order"]}
    st["turn_idx"] = 0

    await context.bot.send_message(
        chat_id,
        f"🧩 *Đoán chữ bắt đầu!*\nCâu hỏi: _{q['question']}_\n"
        "Gõ câu trả lời của bạn. Mỗi người có **3 lượt**.",
        parse_mode="Markdown"
    )

def is_user_turn(st: Dict, uid: int) -> bool:
    if 0 in st["order"]:
        return True  # single mode
    return st["order"][st["turn_idx"]] == uid

def next_turn(st: Dict):
    # loại ai hết lượt
    st["order"] = [uid for uid in st["order"] if st["lives"].get(uid, 0) > 0]
    if not st["order"]:
        return
    st["turn_idx"] = (st["turn_idx"] + 1) % len(st["order"])

async def handle_text_doan_chu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = GAMES.get(chat_id)
    if not st or st.get("type") != "doan" or not st.get("started") or not st.get("current"):
        return

    uid = update.effective_user.id if update.effective_user else 0
    text = (update.effective_message.text or "").strip()

    if uid not in st["lives"]:
        # người ngoài bàn -> vẫn cho đoán nhưng không tính lượt
        pass
    else:
        if not is_user_turn(st, uid):
            await update.effective_message.reply_text("Chưa tới lượt bạn nha.")
            return

    ans = st["current"]["answer"]
    if text.lower() == ans.lower():
        await update.effective_message.reply_text("🎉 *Chính xác!* Bạn đã thắng.", parse_mode="Markdown")
        # reset bàn
        del GAMES[chat_id]
        return

    # sai
    await update.effective_message.reply_text(random_taunt())
    if uid in st["lives"]:
        st["lives"][uid] -= 1
        if st["lives"][uid] <= 0:
            await update.effective_message.reply_text(f"⛔ {update.effective_user.first_name} đã *hết lượt*.")
        next_turn(st)

    # nếu tất cả hết lượt
    if all(v <= 0 for v in st["lives"].values()):
        await update.effective_message.reply_text(f"💀 Hết lượt cả bàn. Đáp án đúng là: *{ans}*.", parse_mode="Markdown")
        del GAMES[chat_id]

# ===================== WIRING BOT =====================

from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

def build_bot() -> Application:
    # Tạo Application (không dùng Updater cũ khi chạy webhook)
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .updater(None)          # quan trọng: tránh Updater cũ gây lỗi
        .build()                # <<< THÊM build() để có Application
    )

    # ======= lệnh =======
    application.add_handler(CommandHandler("start", start))

    # ======= menu/callback =======
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(other_callback, pattern=r"^other:"))

    # ======= router text cho 2 game =======
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # ======= job: kiểm tra lobby mỗi 5s =======
    application.job_queue.run_repeating(periodic_check, interval=5, first=5)

    return application
# ===================== FASTAPI LIFECYCLE =====================
@app.on_event("startup")
async def on_startup():
    global application
    # load cache
    await load_dict_cache()
    await load_clue_bank()

    application = build_bot()
    # init + start PTB
    await application.initialize()
    await application.start()

    # set webhook (idempotent)
    if BOT_TOKEN and BASE_URL:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                url = (
                    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
                    f"?url={BASE_URL}/webhook&secret_token={WEBHOOK_SECRET}"
                )
                await client.get(url)
        except Exception:
            pass

@app.on_event("shutdown")
async def on_shutdown():
    if application:
        try:
            await application.stop()
            await application.shutdown()
        except Exception:
            pass

# ===================== ROUTES =====================
@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Bảo vệ: secret token phải khớp (nếu Telegram có gửi)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        # vẫn trả 200 để không retry vô hạn, nhưng bỏ qua
        return Response(status_code=status.HTTP_200_OK)

    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    if not application:
        return {"ok": True}

    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception:
        # không để crash app
        pass

    return {"ok": True}
