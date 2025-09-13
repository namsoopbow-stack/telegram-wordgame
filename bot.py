# bot.py
import os, json, re, random, asyncio, logging, aiohttp
from dataclasses import dataclass, field
from typing import Dict, Set, List, Optional, Tuple
from unidecode import unidecode

from telegram import Update, Chat, MessageEntity
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters
)

# ============ Cấu hình & logging ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wordchain")

BOT_TOKEN      = os.getenv("TELEGRAM_TOKEN")
DICT_FILE      = os.getenv("DICT_FILE", "dict_vi.txt")
LOBBY_SECONDS  = int(os.getenv("LOBBY_SECONDS", "60"))
TURN_SECONDS   = int(os.getenv("TURN_SECONDS", "30"))
GIST_ID        = os.getenv("GIST_ID")
GIST_TOKEN     = os.getenv("GIST_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN")

# ============ Tiện ích từ vựng ============
def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_two_words_vn(phrase: str) -> bool:
    # Hai “từ” tách bằng khoảng trắng, bỏ ký tự thừa ở đầu/cuối
    phrase = clean_spaces(phrase)
    parts = phrase.split(" ")
    return len(parts) == 2 and all(p for p in parts)

def first_word(phrase: str) -> str:
    return clean_spaces(phrase).split(" ")[0]

def last_word(phrase: str) -> str:
    return clean_spaces(phrase).split(" ")[-1]

def same_word(a: str, b: str) -> bool:
    # So sánh theo yêu cầu: phân biệt dấu (để đúng nghĩa), không phân biệt hoa/thường
    return clean_spaces(a).lower() == clean_spaces(b).lower()

# ============ Bộ từ điển/Cache ============
class PhraseStore:
    """Quản lý từ điển offline + cache + cập nhật Gist."""
    def __init__(self, dict_file: str):
        self.dict_file = dict_file
        self.phrases: Set[str] = set()
        self._load_local_file()

    def _load_local_file(self):
        try:
            with open(self.dict_file, "r", encoding="utf-8") as f:
                for line in f:
                    s = clean_spaces(line)
                    if s:
                        self.phrases.add(s)
            log.info("Đã nạp %d cụm từ offline.", len(self.phrases))
        except FileNotFoundError:
            log.warning("Không tìm thấy %s, bắt đầu với bộ từ điển rỗng.", self.dict_file)

    def contains(self, phrase: str) -> bool:
        return clean_spaces(phrase) in self.phrases

    async def online_exists(self, phrase: str) -> bool:
        """Kiểm tra Wiktionary có trang đúng cụm từ hay không."""
        title = clean_spaces(phrase)
        if not title:
            return False
        url = "https://vi.wiktionary.org/w/api.php"
        params = {"action": "query", "format": "json", "titles": title}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                async with sess.get(url, params=params) as r:
                    data = await r.json()
            pages = data.get("query", {}).get("pages", {})
            # Có pageid và không phải -1 nghĩa là tồn tại
            exists = any(pid != "-1" for pid in pages.keys())
            return bool(exists)
        except Exception as e:
            log.warning("Lỗi Wiktionary: %s", e)
            return False

    async def persist_new_phrase(self, phrase: str):
        """Thêm cụm mới vào RAM, file local (nếu có quyền), và Gist (nếu cấu hình)."""
        phrase = clean_spaces(phrase)
        if not phrase or phrase in self.phrases:
            return
        self.phrases.add(phrase)

        # Ghi nối file local (không bắt buộc)
        try:
            with open(self.dict_file, "a", encoding="utf-8") as f:
                f.write(phrase + "\n")
        except Exception as e:
            log.warning("Không ghi được file local: %s", e)

        # Đẩy lên Gist nếu có cấu hình
        if GIST_ID and GIST_TOKEN:
            try:
                await self._append_to_gist(phrase)
            except Exception as e:
                log.warning("Không cập nhật Gist: %s", e)

    async def _append_to_gist(self, phrase: str):
        """Tải nội dung Gist, nối thêm dòng, rồi PATCH lại."""
        api = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}",
                   "Accept": "application/vnd.github+json"}
        # Lấy nội dung hiện tại
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(api, headers=headers) as r:
                gist = await r.json()
        # Chọn file đầu tiên trong gist để cập nhật (hoặc file có tên 'dict_offline.txt' nếu có)
        files = gist.get("files", {})
        target_name = None
        if "dict_offline.txt" in files:
            target_name = "dict_offline.txt"
        elif files:
            target_name = list(files.keys())[0]
        else:
            # Gist trống -> tạo file mặc định
            target_name = "dict_offline.txt"

        old_content = files.get(target_name, {}).get("content", "") if files else ""
        new_content = (old_content.rstrip("\n") + ("\n" if old_content else "") + phrase + "\n")

        payload = {"files": {target_name: {"content": new_content}}}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.patch(api, headers=headers, json=payload) as r:
                ok = (200 <= r.status < 300)
        if ok:
            log.info("Đã cập nhật Gist (%s).", target_name)

PHRASES = PhraseStore(DICT_FILE)

async def is_valid_phrase(phrase: str) -> Tuple[bool, str]:
    """Kiểm tra cụm 2 từ có nghĩa: offline trước, rồi online; nếu đậu online thì lưu vĩnh viễn."""
    s = clean_spaces(phrase)
    if not is_two_words_vn(s):
        return False, "Câu phải gồm **cụm 2 từ** (ví dụ: “cá heo”, “quên đi”)."

    if PHRASES.contains(s):
        return True, "OK (offline)."

    # Thử online
    online = await PHRASES.online_exists(s)
    if online:
        # Cache vĩnh viễn
        await PHRASES.persist_new_phrase(s)
        return True, "OK (online)."

    return False, "Cụm không có nghĩa (không tìm thấy)."

# Chọn câu BOT trả lời khi solo
def bot_candidates(prefix: str, used: Set[str]) -> List[str]:
    pref = clean_spaces(prefix)
    out = [p for p in PHRASES.phrases
           if first_word(p).lower() == pref.lower() and p not in used]
    random.shuffle(out)
    return out

# ============ Trạng thái game ============
@dataclass
class GameState:
    chat_id: int
    players: List[int] = field(default_factory=list)     # danh sách user_id theo lượt
    player_names: Dict[int, str] = field(default_factory=dict)
    started: bool = False
    vs_bot: bool = False
    required_prefix: Optional[str] = None
    last_phrase: Optional[str] = None
    used: Set[str] = field(default_factory=set)
    join_job: Optional[asyncio.Task] = None
    turn_deadline: Optional[float] = None
    turn_owner: Optional[int] = None

    def reset_turn(self):
        self.turn_deadline = None
        self.turn_owner = None

GAMES: Dict[int, GameState] = {}  # chat_id -> GameState

def mention_html(uid: int, name: str) -> str:
    name = clean_spaces(name) or "người chơi"
    return f'<a href="tg://user?id={uid}">{name}</a>'

REMINDERS = [
    "Nhanh lên nào, thời gian không chờ ai!",
    "Suy nghĩ chi nữa, gõ câu **2 từ** đi!",
    "Còn chút xíu thời gian thôi!",
    "Đừng ngắm màn hình nữa, đánh chữ đi!",
    "IQ tới đây thôi à? Nhanh tay lên!",
    "Chậm quá là **bay màu** đấy!",
    "Vẫn chưa có câu à? Mạnh dạn lên!",
    "Gấp gấp gấp! Chuỗi sắp gãy rồi!",
    "Cơ hội không chờ đợi, quất!",
    "Nhanh như chớp nào!!",
]

ELIM_REASONS = {
    "timeout": "Hết giờ lượt! Mời người kế tiếp.",
    "format":  "Sai định dạng: cần **cụm 2 từ**.",
    "chain":   "Sai luật chuỗi: từ đầu phải bằng **từ cuối** của câu trước.",
    "meaning": "Cụm không có nghĩa (tra không thấy).",
    "repeat":  "Cụm đã dùng trong ván đấu, không được lặp.",
}

# ============ Điều phối game ============

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(
        "Chào nhóm! Dùng <b>/newgame</b> để mở sảnh, <b>/join</b> để tham gia."
    )

async def cmd_newgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    st = GameState(chat_id=chat.id)
    GAMES[chat.id] = st
    await update.effective_message.reply_html(
        f"🎮 Mở sảnh! Gõ <b>/join</b> để tham gia. "
        f"🔔 Tự bắt đầu sau <b>{LOBBY_SECONDS}s</b> nếu có người tham gia."
    )
    # Đếm ngược sảnh
    async def lobby_countdown():
        await asyncio.sleep(LOBBY_SECONDS)
        st2 = GAMES.get(chat.id)
        if not st2 or st2.started:
            return
        if len(st2.players) == 0:
            await ctx.bot.send_message(chat.id, "⛔ Không ai tham gia. Huỷ ván.")
            GAMES.pop(chat.id, None)
            return
        await start_match(ctx.bot, st2)
    st.join_job = asyncio.create_task(lobby_countdown())

async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    st = GAMES.get(chat.id)
    if not st:
        await update.effective_message.reply_text("Chưa mở sảnh. Dùng /newgame trước.")
        return
    if st.started:
        await update.effective_message.reply_text("Đang có ván rồi, chờ ván sau nha.")
        return
    if user.id in st.players:
        await update.effective_message.reply_text("Bạn đã tham gia rồi!")
        return
    st.players.append(user.id)
    st.player_names[user.id] = user.full_name
    await update.effective_message.reply_html(
        f"✅ {mention_html(user.id, user.full_name)} đã tham gia!"
    )

async def start_match(bot, st: GameState):
    st.started = True
    # Quyết định chế độ
    if len(st.players) == 1:
        st.vs_bot = True
        st.turn_owner = st.players[0]
        await bot.send_message(
            st.chat_id,
            f"👤 Chỉ 1 người → chơi với BOT.\n"
            "✨ Lượt đầu: gửi <b>cụm 2 từ có nghĩa</b> bất kỳ. Sau đó đối tiếp bằng <b>từ cuối</b>."
            , parse_mode=ParseMode.HTML
        )
    else:
        st.vs_bot = False
        random.shuffle(st.players)
        st.turn_owner = st.players[0]
        names = ", ".join(mention_html(uid, st.player_names[uid]) for uid in st.players)
        await bot.send_message(
            st.chat_id,
            f"👥 {len(st.players)} người tham gia.\nNgười đi trước: "
            f"{mention_html(st.turn_owner, st.player_names[st.turn_owner])}\n"
            "✨ Lượt đầu: gửi <b>cụm 2 từ có nghĩa</b> bất kỳ. Sau đó đối tiếp bằng <b>từ cuối</b>.",
            parse_mode=ParseMode.HTML
        )
    await begin_turn(bot, st)

async def begin_turn(bot, st: GameState):
    st.turn_deadline = asyncio.get_event_loop().time() + TURN_SECONDS
    owner = st.turn_owner
    if not owner:
        return
    # Nhắc giữa chừng & sát giờ
    async def reminders():
        await asyncio.sleep(max(1, TURN_SECONDS // 2))
        if st.turn_owner == owner and st.turn_deadline and asyncio.get_event_loop().time() < st.turn_deadline:
            await bot.send_message(st.chat_id, f"⏳ {random.choice(REMINDERS)}")
        remain = st.turn_deadline - asyncio.get_event_loop().time()
        if remain > 5:
            await asyncio.sleep(remain - 5)
        if st.turn_owner == owner and st.turn_deadline and asyncio.get_event_loop().time() < st.turn_deadline:
            await bot.send_message(st.chat_id, "⏰ Còn 5 giây!")
        # Hết giờ
        await asyncio.sleep(max(0, st.turn_deadline - asyncio.get_event_loop().time()))
        if st.turn_owner == owner and st.turn_deadline:
            await eliminate_player(bot, st, owner, "timeout")

    asyncio.create_task(reminders())

async def eliminate_player(bot, st: GameState, uid: int, reason_key: str):
    if uid in st.players:
        st.players.remove(uid)
    await bot.send_message(
        st.chat_id,
        f"❌ {mention_html(uid, st.player_names.get(uid,'người chơi'))} bị loại. {ELIM_REASONS[reason_key]}",
        parse_mode=ParseMode.HTML
    )
    st.reset_turn()
    # Kết thúc hay tiếp tục
    if st.vs_bot:
        await bot.send_message(st.chat_id, "🏁 Hết người chơi. Kết thúc ván.")
        GAMES.pop(st.chat_id, None)
        return
    if len(st.players) <= 1:
        if st.players:
            await bot.send_message(st.chat_id, f"🏆 {mention_html(st.players[0], st.player_names[st.players[0]])} thắng!",
                                   parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(st.chat_id, "🏁 Không còn người chơi. Kết thúc ván.")
        GAMES.pop(st.chat_id, None)
        return
    # Chuyển lượt
    st.turn_owner = st.players[0]
    st.players = st.players[1:] + [st.turn_owner]
    await bot.send_message(st.chat_id,
        f"👉 Lượt của {mention_html(st.turn_owner, st.player_names[st.turn_owner])}",
        parse_mode=ParseMode.HTML
    )
    await begin_turn(bot, st)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    text = clean_spaces(update.effective_message.text or "")
    st = GAMES.get(chat.id)

    # Không trong ván → bỏ qua
    if not st or not st.started:
        return

    # Chỉ nhận message của người đến lượt (với chế độ nhiều người) hoặc người solo
    if not st.vs_bot and user.id != st.turn_owner:
        return
    if st.vs_bot and user.id != st.turn_owner:
        return

    # Kiểm tra theo luật
    # 1) Cụm 2 từ
    if not is_two_words_vn(text):
        await ctx.bot.send_message(chat.id, f"❌ {ELIM_REASONS['format']}")
        if not st.vs_bot:
            await eliminate_player(ctx.bot, st, user.id, "format")
        else:
            # Solo: cho thử tiếp (không loại), chỉ cảnh báo
            await begin_turn(ctx.bot, st)
        return

    # 2) Luật chuỗi (nếu không phải nước đầu)
    if st.required_prefix:
        if not same_word(first_word(text), st.required_prefix):
            await ctx.bot.send_message(chat.id, f"❌ {ELIM_REASONS['chain']}")
            if not st.vs_bot:
                await eliminate_player(ctx.bot, st, user.id, "chain")
            else:
                await begin_turn(ctx.bot, st)
            return

    # 3) Trùng lặp trong ván?
    if text in st.used:
        await ctx.bot.send_message(chat.id, f"❌ {ELIM_REASONS['repeat']}")
        if not st.vs_bot:
            await eliminate_player(ctx.bot, st, user.id, "repeat")
        else:
            await begin_turn(ctx.bot, st)
        return

    # 4) Có nghĩa?
    ok, why = await is_valid_phrase(text)
    if not ok:
        await ctx.bot.send_message(chat.id, f"❌ {why}")
        if not st.vs_bot:
            await eliminate_player(ctx.bot, st, user.id, "meaning")
        else:
            await begin_turn(ctx.bot, st)
        return

    # Câu hợp lệ
    st.used.add(text)
    st.last_phrase = text
    st.required_prefix = last_word(text)
    st.reset_turn()

    # — Chế độ SOLO: BOT đối lại
    if st.vs_bot:
        # BOT đối ngay
        cands = bot_candidates(st.required_prefix, st.used)
        if not cands:
            await ctx.bot.send_message(chat.id, "🤖 BOT chịu! Bạn thắng 👑")
            GAMES.pop(chat.id, None)
            return
        bot_phrase = cands[0]
        st.used.add(bot_phrase)
        st.last_phrase = bot_phrase
        st.required_prefix = last_word(bot_phrase)
        await ctx.bot.send_message(chat.id, f"🤖 {bot_phrase}\n👉 Tiếp tục bằng: <b>{st.required_prefix}</b>",
                                   parse_mode=ParseMode.HTML)
        # Lượt lại về người chơi
        await begin_turn(ctx.bot, st)
        return

    # — Chế độ NHIỀU NGƯỜI: chuyển lượt bình thường
    await ctx.bot.send_message(chat.id, f"✅ Hợp lệ. 👉 Từ bắt đầu tiếp theo: <b>{st.required_prefix}</b>",
                               parse_mode=ParseMode.HTML)
    # Chuyển lượt vòng tròn
    st.players = st.players[1:] + [st.players[0]]
    st.turn_owner = st.players[0]
    await ctx.bot.send_message(chat.id,
        f"👉 Lượt của {mention_html(st.turn_owner, st.player_names[st.turn_owner])}",
        parse_mode=ParseMode.HTML
    )
    await begin_turn(ctx.bot, st)

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    st = GAMES.pop(chat.id, None)
    if st and st.join_job:
        st.join_job.cancel()
    await update.effective_message.reply_text("Đã huỷ ván hiện tại.")

# ============ Bootstrap ============
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    return app

# ============ Chạy trực tiếp / dùng webhook ============
async def initialize():
    # Không cần làm gì thêm ở đây; hook để tương thích webhook.py
    pass

async def start():
    # Không dùng polling trong Render (xài webhook.py). Giữ để chạy local.
    app = build_app()
    await app.initialize()
    await app.start()
    log.info("Bot started (polling). Ctrl+C to stop.")
    await app.updater.start_polling()
    await app.updater.idle()

async def stop():
    pass

# Để chạy local: python bot.py
if __name__ == "__main__":
    import asyncio
    asyncio.run(start())
