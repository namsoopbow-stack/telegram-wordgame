# bot.py
import os, random, re, asyncio
from collections import defaultdict, deque
from typing import List, Set, Dict, Optional, Tuple

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, AIORateLimiter,
    CommandHandler, MessageHandler, filters, ContextTypes
)
from unidecode import unidecode

# ========= Cấu hình qua ENV =========
ROUND_SECONDS  = int(os.getenv("ROUND_SECONDS", "60"))
HALF_WARN      = int(os.getenv("HALF_WARN", "30"))
DICT_FILE      = os.getenv("DICT_FILE", "dict_vi.txt")       # bộ cụm 2 từ (bạn đã có)
VERBS_FILE     = os.getenv("VERBS_FILE", "verbs_vi.txt")     # list động từ (bổ sung)
BOT_TOKEN      = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN")

# ========= Tải từ điển =========
def norm_text(s: str) -> str:
    # chuẩn hoá để so trùng (không bỏ dấu bản gốc khi hiển thị)
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def key_no_tone(s: str) -> str:
    s = norm_text(s)
    s = unidecode(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def load_lines(path: str) -> List[str]:
    if not os.path.exists(path): return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if t:
                out.append(t)
    return out

# dict cụm 2 từ
RAW_PHRASES: List[str] = load_lines(DICT_FILE)
PHRASES_SET: Set[str] = set(key_no_tone(x) for x in RAW_PHRASES)

# verbs
RAW_VERBS: List[str] = load_lines(VERBS_FILE)
VERBS_SET: Set[str] = set(key_no_tone(x) for x in RAW_VERBS)

def split2(s: str) -> Optional[Tuple[str, str]]:
    t = norm_text(s)
    parts = t.split(" ")
    if len(parts) != 2: return None
    return parts[0], parts[1]

def is_action_phrase(text: str) -> bool:
    """Hợp lệ nếu:
       - đúng 2 từ
       - (a) cả cụm có trong DICT_FILE (khuyến nghị bạn chỉ giữ cụm động từ), hoặc
       - (b) từ1 ∈ verbs & từ2 ∈ verbs (xem như cụm hành động)
    """
    pair = split2(text)
    if not pair: return False
    a, b = pair
    # a+b trong dict cụm 2 từ?
    if key_no_tone(f"{a} {b}") in PHRASES_SET:
        return True
    # fallback: cả 2 đều là động từ
    return (key_no_tone(a) in VERBS_SET) and (key_no_tone(b) in VERBS_SET)

# ========= Trạng thái game theo chat =========
class GameState:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.is_lobby_open = False
        self.players: List[int] = []
        self.player_names: Dict[int, str] = {}
        self.turn_idx = 0
        self.vs_bot = False
        self.active = False
        self.last_phrase: Optional[str] = None
        self.used_keys: Set[str] = set()
        self.countdown_job = None
        self.reminder_job = None
        self.round_timeout_job = None
        self.guess_left: Dict[int, int] = defaultdict(lambda: 1)  # mỗi người 1 lượt tại một thời điểm
        self.current_player: Optional[int] = None

    def reset_round(self):
        self.turn_idx = 0
        self.active = False
        self.last_phrase = None
        self.used_keys.clear()
        self.current_player = None

    def next_player(self):
        if not self.players: return None
        self.turn_idx = (self.turn_idx + 1) % len(self.players)
        self.current_player = self.players[self.turn_idx]
        return self.current_player

    def current_player_id(self):
        return self.current_player

# tất cả chat
GAMES: Dict[int, GameState] = {}

# ========= Countdown / nhắc giờ =========
async def start_countdown(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = GAMES[chat_id]
    # nhắc mốc 60, 30, 10
    await context.bot.send_message(chat_id, f"🎮 Sảnh mở! /join để tham gia. **{ROUND_SECONDS}s** nữa vào trận.")
    # nửa thời gian
    await asyncio.sleep(max(0, ROUND_SECONDS - HALF_WARN))
    if game.is_lobby_open:
        await context.bot.send_message(chat_id, f"⏳ Còn **{HALF_WARN}s** nữa.")
    # 10s cuối
    remain = ROUND_SECONDS - (ROUND_SECONDS - HALF_WARN) - 20
    if remain > 0: await asyncio.sleep(remain)
    if game.is_lobby_open:
        await context.bot.send_message(chat_id, "⏳ **Còn 20s** nữa…")
    await asyncio.sleep(10)
    if game.is_lobby_open:
        await context.bot.send_message(chat_id, "⏳ **Còn 10s** nữa…")
    await asyncio.sleep(10)
    # Hết countdown → bắt đầu nếu đủ người
    if not game.is_lobby_open: 
        return
    if len(game.players) == 0:
        await context.bot.send_message(chat_id, "❌ Không ai tham gia. Hủy sảnh.")
        game.is_lobby_open = False
        return
    # quyết định chế độ
    if len(game.players) == 1:
        game.vs_bot = True
        await context.bot.send_message(
            chat_id,
            f"🤖 Chỉ có 1 người tham gia. Bắt đầu **đấu với bot**!"
        )
    else:
        game.vs_bot = False
        await context.bot.send_message(
            chat_id,
            f"👥 Có {len(game.players)} người. Bắt đầu! Bot sẽ làm trọng tài."
        )
    game.is_lobby_open = False
    await begin_round(context, chat_id)

async def begin_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = GAMES[chat_id]
    game.reset_round()
    if not game.players:
        await context.bot.send_message(chat_id, "❌ Không có người chơi.")
        return

    # chọn người đi đầu
    game.current_player = game.players[game.turn_idx]
    name = game.player_names.get(game.current_player, "người chơi")

    # tạo cụm mở đầu (để người chơi đối) – chọn ngẫu nhiên từ DICT_FILE (ưu tiên cụm trong dict)
    if RAW_PHRASES:
        seed = random.choice(RAW_PHRASES)
    else:
        # fallback nếu bạn chưa có dict cụm, chọn 2 verbs ngẫu nhiên cho đúng luật
        if len(RAW_VERBS) < 2:
            await context.bot.send_message(chat_id, "⚠️ Chưa có dữ liệu từ điển.")
            return
        seed = f"{random.choice(RAW_VERBS)} {random.choice(RAW_VERBS)}"

    game.last_phrase = seed
    game.used_keys.add(key_no_tone(seed))

    await context.bot.send_message(
        chat_id,
        f"🎯 Cụm mở đầu: **{seed}**\n"
        f"👉 {name} đi trước. Gửi cụm **2 từ** (hành động) sao cho **từ đầu** trùng **từ cuối** của cụm trước."
    )

    # set timer cho lượt đầu
    await set_turn_timers(context, chat_id)

async def set_turn_timers(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = GAMES.get(chat_id)
    if not game: return
    # nhắc 30s
    async def half_warn_cb(ctx: ContextTypes.DEFAULT_TYPE):
        await ctx.bot.send_message(chat_id, random.choice(HALF_WARN_LINES))

    async def timeup_cb(ctx: ContextTypes.DEFAULT_TYPE):
        await on_timeout(ctx, chat_id)

    # chạy song song 2 job “ngủ”
    asyncio.create_task(_sleep_and_call(HALF_WARN, context, half_warn_cb))
    asyncio.create_task(_sleep_and_call(ROUND_SECONDS, context, timeup_cb))

async def _sleep_and_call(seconds: int, context, coro_func):
    await asyncio.sleep(seconds)
    try:
        await coro_func(context)
    except Exception:
        pass

async def on_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = GAMES.get(chat_id)
    if not game or not game.current_player: return
    name = game.player_names.get(game.current_player, "người chơi")
    await context.bot.send_message(chat_id, random.choice(TIMEOUT_LINES).format(name=name))
    # loại người chơi
    await eliminate_or_next(context, chat_id, wrong=True)

async def eliminate_or_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int, wrong: bool):
    game = GAMES.get(chat_id)
    if not game: return
    if wrong:
        # loại player hiện tại
        pid = game.current_player
        if pid in game.players:
            game.players.remove(pid)
        await context.bot.send_message(chat_id, f"❌ {game.player_names.get(pid,'người chơi')} bị loại.")
        if len(game.players) == 0:
            await context.bot.send_message(chat_id, "🏁 Trò chơi kết thúc – không còn ai.")
            return
        # căn lại turn_idx để không nhảy quá
        game.turn_idx = game.turn_idx % len(game.players)

    # nếu chỉ còn 1 người và đang ở chế độ trọng tài → người đó thắng
    if not game.vs_bot and len(game.players) == 1:
        winner = game.players[0]
        await context.bot.send_message(chat_id, f"👑 {game.player_names.get(winner,'người chơi')} thắng cuộc!")
        return

    # qua người tiếp theo
    game.current_player = game.players[game.turn_idx]
    await context.bot.send_message(chat_id, f"👉 Đến lượt {game.player_names.get(game.current_player,'người chơi')}.")

    # set timer mới cho lượt kế
    await set_turn_timers(context, chat_id)

# ========= Bot đánh nếu 1v1 =========
def find_reply(last_phrase: str) -> Optional[str]:
    """Tìm cụm đáp ứng luật: từ1 == last_word(last_phrase)"""
    pair = split2(last_phrase)
    if not pair: return None
    last_w = pair[1]
    target_key = key_no_tone(last_w)

    candidates: List[str] = []
    # ưu tiên cụm trong PHRASES_SET bắt đầu bằng last_w
    for p in RAW_PHRASES:
        sp = split2(p)
        if not sp: continue
        if key_no_tone(sp[0]) == target_key:
            candidates.append(p)
    # fallback: ghép 2 verbs
    if not candidates and RAW_VERBS:
        for v in RAW_VERBS:
            if key_no_tone(v) == target_key:
                # ghép với một verb khác
                tail = random.choice(RAW_VERBS)
                candidates.append(f"{v} {tail}")

    return random.choice(candidates) if candidates else None

# ========= Câu nhắc =========
HALF_WARN_LINES = [
    "⏳ Nhanh lên bạn ơi, thời gian không chờ ai cả!",
    "⏳ Chậm thế? Mau đoán đi chứ!",
    "⏳ IQ chỉ thế thôi sao? Nhanh cái não lên!",
    "⏳ Suy nghĩ gì nữa! Gửi luôn đi!",
    "⏳ Vẫn chưa có kết quả sao?",
    "⏳ Đừng để hết giờ oan nhé!",
    "⏳ Cố lên, cụm 2 từ hành động thôi mà!",
    "⏳ Đếm ngược đấy, lẹ nào!",
    "⏳ Gợi ý: từ đầu phải là từ cuối của cụm trước!",
    "⏳ Hơi bị chậm rồi đó!",
]
TIMEOUT_LINES = [
    "⏰ Hết giờ cho {name}!",
    "⏰ {name} đứng hình 5s… và hết giờ!",
    "⏰ {name} quá chậm, xin chào tạm biệt!",
]

# ========= Handlers =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào nhóm! /newgame để mở sảnh, /join để vào, /stop để dừng.\n"
        f"Luật: đối chữ **2 từ** (cụm **động từ** có nghĩa). Câu sau phải bắt đầu bằng **từ cuối** của câu trước.\n"
        f"Đếm ngược: {ROUND_SECONDS}s, nhắc ở {HALF_WARN}s."
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = GAMES.get(chat_id) or GameState(chat_id)
    GAMES[chat_id] = game

    if game.is_lobby_open:
        await update.message.reply_text("Sảnh đang mở rồi, /join đi bạn ơi!")
        return
    if game.active:
        await update.message.reply_text("Đang có trận, /stop nếu muốn dừng.")
        return

    # mở sảnh & countdown
    game.is_lobby_open = True
    game.players = []
    game.player_names = {}
    game.vs_bot = False
    await update.message.reply_text("🎮 Sảnh mở! /join để tham gia. Sẽ bắt đầu sau 60s.")
    asyncio.create_task(start_countdown(context, chat_id))

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = GAMES.get(chat_id)
    if not game or not game.is_lobby_open:
        await update.message.reply_text("Chưa mở sảnh. Dùng /newgame nhé.")
        return
    if user.id in game.players:
        await update.message.reply_text("Bạn đã tham gia rồi.")
        return
    game.players.append(user.id)
    game.player_names[user.id] = (user.full_name or f"user_{user.id}")
    await update.message.reply_text(f"✅ {user.full_name} đã vào. Hiện có {len(game.players)} người.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = GAMES.get(chat_id)
    if not game:
        await update.message.reply_text("Chưa có trận nào.")
        return
    GAMES.pop(chat_id, None)
    await update.message.reply_text("🛑 Đã dừng trận hiện tại.")

# kiểm tra câu trả lời
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    chat_id = update.effective_chat.id
    game = GAMES.get(chat_id)
    if not game or game.is_lobby_open or game.current_player is None:
        return

    user = update.effective_user
    if user.id != game.current_player:
        return  # không phải lượt của bạn

    text = norm_text(update.message.text)
    if not split2(text):
        await update.message.reply_text("❌ Phải là **cụm 2 từ**.")
        return

    # luật xâu: từ đầu phải trùng từ cuối trước
    last_a, last_b = split2(game.last_phrase)
    now_a, now_b = split2(text)
    if key_no_tone(now_a) != key_no_tone(last_b):
        await update.message.reply_text("❌ Sai luật: từ đầu phải trùng **từ cuối** cụm trước.")
        return

    # không lặp
    if key_no_tone(text) in game.used_keys:
        await update.message.reply_text("❌ Cụm này đã dùng trong vòng này.")
        return

    # kiểm tra “cụm hành động”
    if not is_action_phrase(text):
        await update.message.reply_text("❌ Không phải **cụm động từ** có nghĩa.")
        return

    # hợp lệ!
    game.last_phrase = text
    game.used_keys.add(key_no_tone(text))
    await update.message.reply_text(f"✅ Hợp lệ: **{text}**")

    if game.vs_bot:
        # bot đánh
        await asyncio.sleep(0.8)
        bot_reply = find_reply(game.last_phrase)
        if not bot_reply or key_no_tone(bot_reply) in game.used_keys:
            await context.bot.send_message(chat_id, "🤖 Thua rồi… bạn giỏi quá! 🏆")
            return
        game.last_phrase = bot_reply
        game.used_keys.add(key_no_tone(bot_reply))
        await context.bot.send_message(chat_id, f"🤖 Bot: **{bot_reply}**")
        # tới lượt người chơi lại
        await set_turn_timers(context, chat_id)
    else:
        # chuyển lượt sang người kế
        game.next_player()
        await set_turn_timers(context, chat_id)

# ========= Build App =========
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_message))
    return app

# local run
if __name__ == "__main__":
    app = build_app()
    app.run_polling(close_loop=False)
