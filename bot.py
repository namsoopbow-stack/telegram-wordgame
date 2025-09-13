# bot.py
import os, json, random, re, asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

import httpx
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---------- Cấu hình Gist ----------
GIST_ID    = os.getenv("GIST_ID")
GIST_TOKEN = os.getenv("GIST_TOKEN")

DICT_FILE = "dict_offline.txt"
BANK_FILE = "guess_clue_bank.json"

if not GIST_ID or not GIST_TOKEN:
    # Cho phép chạy local không có Gist (nhưng trên Render nên set)
    print("WARN: Missing GIST_ID or GIST_TOKEN -> only online check & empty question bank.")

GITHUB_API = f"https://api.github.com/gists/{GIST_ID}"

# ---------- Câu cà khịa ----------
TAUNT_WRONG = [
    "Sai bét! Cụm này tớ không thấy nghĩa đâu 😝",
    "Trật lất, thử câu khác xem nào!",
    "Lệch sóng rồi bạn ơi 😅",
    "Hơi sai sai… kiếm cụm chuẩn hơn nhé!",
    "Không qua được cửa kiểm tra nghĩa rồi 🧱",
    "Chưa hợp lệ đâu, đổi bài nha!",
    "Cụm này vô nghĩa thì phải? 🤔",
    "Rớt môn từ vựng rồi 🙈",
    "Không ổn, xin mời lượt kế tiếp!",
    "Bị trọng tài bắt lỗi! 🚨",
    "Bạn ơi, cụm phải có nghĩa rõ ràng nha!",
    "Còn thiếu muối nghĩa đó 😆",
    "Không tìm thấy nghĩa đáng tin.",
    "Tạch! Đổi chiến thuật lẹ đi!",
    "Cụm không hợp lệ, nghỉ chơi một vòng nhé!"
]

TAUNT_TIMEOUT = [
    "Hết giờ! Nhanh như chớp cơ mà ⏰",
    "Ngơ ngác nhìn đồng hồ… loại! 😴",
    "Chậm một nhịp thôi là xong!",
    "Hết 30 giây, tiếc ghê!",
    "Đồng hồ không chờ ai đâu nha!",
    "Im lặng là… bị loại 😬",
    "Không kịp rồi, nhường lượt!",
    "Gió cuốn đi cả câu trả lời 🌬️",
    "Ủa còn đó không? Hết giờ mất rồi!",
    "Thời gian là vàng, lần sau nhanh lên nhé!"
]

REMINDERS = [
    "Còn 30 giây nhé! ⏳",
    "Nhanh nào, còn 30s!",
    "Chuẩn bị bấm gửi đi chứ!",
    "Thời gian trôi nhanh lắm đó!",
    "Đừng để đối thủ vượt mặt!",
    "Cơ hội không chờ đợi ai!",
    "Gõ nhanh nào, còn 30 giây!",
    "Sắp hết giờ rồi!",
    "Đếm ngược bắt đầu…",
    "30s nữa là hết lượt nha!"
]

# ---------- Tiện ích Gist ----------
class GistClient:
    def __init__(self, gist_id: str, token: str):
        self.gist_id = gist_id
        self.token = token
        self.session = httpx.AsyncClient(timeout=15)

    async def _get(self) -> dict:
        r = await self.session.get(GITHUB_API, headers={"Authorization": f"token {self.token}"})
        r.raise_for_status()
        return r.json()

    async def read_file(self, filename: str) -> str:
        try:
            data = await self._get()
            file = data["files"].get(filename)
            if not file:
                return ""
            # tải raw
            raw_url = file["raw_url"]
            r = await self.session.get(raw_url)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print("Gist read error:", e)
            return ""

    async def write_file(self, filename: str, content: str) -> bool:
        try:
            payload = {"files": {filename: {"content": content}}}
            r = await self.session.patch(
                GITHUB_API,
                json=payload,
                headers={"Authorization": f"token {self.token}"}
            )
            r.raise_for_status()
            return True
        except Exception as e:
            print("Gist write error:", e)
            return False

# ---------- Từ điển (cache offline + online) ----------
class VietDict:
    def __init__(self, gist: Optional[GistClient]):
        self.gist = gist
        self.cache: Set[str] = set()

    async def load(self):
        if not self.gist:
            return
        txt = await self.gist.read_file(DICT_FILE)
        if not txt:
            return
        try:
            # chấp nhận JSON list hoặc mỗi dòng 1 cụm
            if txt.strip().startswith('['):
                arr = json.loads(txt)
            else:
                arr = [line.strip() for line in txt.splitlines() if line.strip()]
            self.cache = set(arr)
        except Exception as e:
            print("parse dict_offline error:", e)

    async def persist(self):
        if not self.gist:
            return
        content = json.dumps(sorted(self.cache), ensure_ascii=False, indent=2)
        await self.gist.write_file(DICT_FILE, content)

    async def is_valid(self, phrase: str) -> bool:
        p = self.normalize_phrase(phrase)
        if not p or len(p.split()) != 2:
            return False
        if p in self.cache:
            return True
        # tra online
        ok = await self.check_online(phrase)
        if ok:
            self.cache.add(p)
            await self.persist()
        return ok

    @staticmethod
    def normalize_phrase(s: str) -> str:
        # giữ dấu, chuẩn hoá khoảng trắng
        s = re.sub(r"\s+", " ", (s or "").strip())
        return s

    async def check_online(self, phrase: str) -> bool:
        """Tra trên tratu.soha.vn: nếu có trang nghĩa/từ đồng dạng → coi là hợp lệ."""
        q = self.normalize_phrase(phrase)
        if not q:
            return False
        urls = [
            f"http://tratu.soha.vn/dict/vn_vn/{httpx.utils.quote(q, safe='')}",
            f"http://tratu.soha.vn/dict/vn_vn/search/{httpx.utils.quote(q, safe='')}",
        ]
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as s:
            for u in urls:
                try:
                    r = await s.get(u, follow_redirects=True)
                    if r.status_code != 200:
                        continue
                    html = r.text.lower()
                    if "không tìm thấy" in html or "khong tim thay" in html:
                        continue
                    soup = BeautifulSoup(r.text, "html5lib")
                    # heuristics: có khối kết quả, tiêu đề từ, hoặc danh sách nghĩa
                    if soup.find(class_=re.compile("(result|definition|short|inner)")) \
                       or soup.find("h2") or soup.find("h3"):
                        return True
                except Exception:
                    continue
        return False

# ---------- Ngân hàng câu hỏi đoán chữ ----------
class GuessBank:
    def __init__(self, gist: Optional[GistClient]):
        self.gist = gist
        self.items: List[dict] = []

    async def load(self):
        if not self.gist:
            return
        txt = await self.gist.read_file(BANK_FILE)
        if not txt:
            self.items = []
            return
        try:
            self.items = json.loads(txt)
        except Exception as e:
            print("parse guess_clue_bank error:", e)
            self.items = []

    async def persist(self):
        if not self.gist:
            return
        content = json.dumps(self.items, ensure_ascii=False, indent=2)
        await self.gist.write_file(BANK_FILE, content)

    async def add_item(self, question: str, answer: str, hints: List[str]):
        new_id = (max([it.get("id", 0) for it in self.items]) + 1) if self.items else 1
        self.items.append({"id": new_id, "question": question, "answer": answer, "hints": hints})
        await self.persist()

    def random(self) -> Optional[dict]:
        return random.choice(self.items) if self.items else None

# ---------- State Game ----------
@dataclass
class DoiChuGame:
    lobby: Set[int] = field(default_factory=set)
    live: bool = False
    players: List[int] = field(default_factory=list)
    current_idx: int = 0
    timeout_job_id: Optional[str] = None
    remind_job_id: Optional[str] = None
    last_phrase: Optional[str] = None

@dataclass
class DoanChuGame:
    lobby: Set[int] = field(default_factory=set)
    live: bool = False
    players: List[int] = field(default_factory=list)
    guesses_left: Dict[int, int] = field(default_factory=dict)
    current_idx: int = 0
    question: Optional[dict] = None
    timeout_job_id: Optional[str] = None
    remind_job_id: Optional[str] = None

# ---------- helpers ----------
def menu_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🎮 Game Đối Chữ", callback_data="menu:doi"),
         InlineKeyboardButton("🧩 Game Đoán Chữ", callback_data="menu:doan")]
    ]
    return InlineKeyboardMarkup(kb)

def rules_doi() -> str:
    return (
        "🏓 *Đối Chữ* — luật chơi:\n"
        "• Đối bằng *cụm 2 từ có nghĩa* (giữ nguyên dấu tiếng Việt).\n"
        "• Lượt sau *bắt đầu bằng từ cuối* của lượt trước.\n"
        "• Mỗi lượt *30s*; *sai* hoặc *hết giờ* sẽ *bị loại*.\n"
        "• 1 người tham gia → chơi với BOT. Từ hợp lệ được *lưu cache* để lần sau tra nhanh.\n\n"
        "Lệnh: /new_doi để mở sảnh, /join để tham gia, /begin để bắt đầu ngay."
    )

def rules_doan() -> str:
    return (
        "🧩 *Đoán Chữ* — luật chơi:\n"
        "• Bot rút ngẫu nhiên *câu ca dao/thành ngữ/câu đố* từ ngân hàng.\n"
        "• Mỗi người có *3 lượt đoán*, luân phiên. Hết lượt sẽ *bị loại*.\n"
        "• Có thể thêm câu hỏi mới bằng /addqa.\n\n"
        "Lệnh: /new_doan để mở sảnh, /join để tham gia, /begin để bắt đầu ngay."
    )

def mention(u) -> str:
    return f"[{u.full_name}](tg://user?id={u.id})"

# ---------- Đăng ký handlers ----------
def register_handlers(app: Application):

    # Gist + dữ liệu dùng chung
    gist = GistClient(GIST_ID, GIST_TOKEN) if (GIST_ID and GIST_TOKEN) else None
    vdict = VietDict(gist)
    gbank = GuessBank(gist)

    async def _load_shared():
        await vdict.load()
        await gbank.load()
    app.job_queue.run_once(lambda *_: asyncio.create_task(_load_shared()), when=0)

    # ----- /start
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_chat.send_message(
            "Chọn chế độ bạn muốn chơi:", reply_markup=menu_keyboard()
        )

    # ----- menu nút
    async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if q.data == "menu:doi":
            await q.message.reply_text(rules_doi(), parse_mode="Markdown")
        elif q.data == "menu:doan":
            await q.message.reply_text(rules_doan(), parse_mode="Markdown")

    # ====== ĐỐI CHỮ ======
    async def new_doi(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        data: Dict = context.chat_data.setdefault("doi", {})
        game: DoiChuGame = data.setdefault("game", DoiChuGame())
        if game.live or game.lobby:
            game.lobby = set()
            game.live = False
            game.players = []
        game.lobby.add(update.effective_user.id)
        await update.message.reply_text(
            "🎮 Mở sảnh! Gõ /join để tham gia. 🔔 Tự bắt đầu sau 60s.",
        )
        # bắt đầu đếm lùi 60s
        async def _countdown(_ctx):
            await begin_doi(update, context)
        context.job_queue.run_once(lambda c: asyncio.create_task(_countdown(c)), 60, name=f"lobby_doi_{chat.id}")

    async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        data: Dict = context.chat_data.get("doi") or context.chat_data.get("doan")
        which = "doi" if "doi" in context.chat_data else "doan"
        if not data:
            await update.message.reply_text("Chưa có sảnh nào đang mở. Dùng /new_doi hoặc /new_doan.")
            return
        if which == "doi":
            game: DoiChuGame = data["game"]
            game.lobby.add(update.effective_user.id)
            await update.message.reply_text(f"✅ {mention(update.effective_user)} đã tham gia!", parse_mode="Markdown")
        else:
            game: DoanChuGame = data["game"]
            game.lobby.add(update.effective_user.id)
            await update.message.reply_text(f"✅ {mention(update.effective_user)} đã tham gia!", parse_mode="Markdown")

    async def begin_doi(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        data: Dict = context.chat_data.setdefault("doi", {})
        game: DoiChuGame = data.setdefault("game", DoiChuGame())
        if game.live:
            return
        players = list(game.lobby)
        if not players:
            await chat.send_message("⛔ Không có người tham gia. Huỷ ván.")
            return
        game.live = True
        if len(players) == 1:
            game.players = [players[0], 0]  # 0 đại diện BOT
        else:
            random.shuffle(players)
            game.players = players
        game.current_idx = random.randrange(len(game.players))
        opener = game.players[game.current_idx]
        if opener == 0:
            # BOT mở bằng một cụm phổ biến trong cache hoặc lời mời
            seed = next(iter(vdict.cache)) if vdict.cache else "khai màn"
            game.last_phrase = seed
            await chat.send_message(f"🤖 BOT mở màn: *{seed}*\n{_turn_hint(seed)}",
                                    parse_mode="Markdown")
            await schedule_turn_timeout(context, chat.id, "doi")
        else:
            game.last_phrase = None
            await chat.send_message(
                f"👥 {len([p for p in game.players if p!=0])} người chơi. BOT làm trọng tài.\n"
                f"🎲 Người đi đầu: {mention(await context.bot.get_chat(opener))}\n"
                f"✨ Gửi *cụm 2 từ có nghĩa* bất kỳ để mở nhịp.",
                parse_mode="Markdown"
            )
            await schedule_turn_timeout(context, chat.id, "doi")

    def _turn_hint(prev: str) -> str:
        if not prev:
            return "Gửi *cụm 2 từ có nghĩa* bất kỳ."
        last = prev.split()[-1]
        return f"Lượt sau phải bắt đầu bằng: *{last}*"

    async def schedule_turn_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mode: str):
        # nhắc 30s + timeout 30s
        def _remind(_ctx):
            asyncio.create_task(context.bot.send_message(chat_id, random.choice(REMINDERS)))
        def _timeout(_ctx):
            asyncio.create_task(handle_timeout(chat_id, mode, context))
        context.job_queue.run_once(lambda c: _remind(c), 0)  # thông báo lượt
        context.job_queue.run_once(lambda c: _remind(c), 30, name=f"remind_{mode}_{chat_id}")
        context.job_queue.run_once(lambda c: _timeout(c), 60, name=f"timeout_{mode}_{chat_id}")

    async def handle_timeout(chat_id: int, mode: str, context: ContextTypes.DEFAULT_TYPE):
        if mode == "doi":
            data = context.chat_data.get("doi") or {}
            game: DoiChuGame = (data or {}).get("game")
            if not game or not game.live:
                return
            cur = game.players[game.current_idx]
            if cur == 0:
                await context.bot.send_message(chat_id, "🤖 BOT bỏ lượt (lỗi hệ thống). Tiếp tục!")
            else:
                await context.bot.send_message(chat_id, f"{random.choice(TAUNT_TIMEOUT)}")
                game.players.pop(game.current_idx)
                if not game.players or all(p==0 for p in game.players):
                    await context.bot.send_message(chat_id, "🏁 Hết người chơi. Kết thúc ván.")
                    game.live = False
                    return
                if game.current_idx >= len(game.players):
                    game.current_idx = 0
            await context.bot.send_message(chat_id, _turn_hint(game.last_phrase), parse_mode="Markdown")
            await schedule_turn_timeout(context, chat_id, "doi")
        else:  # doan
            data = context.chat_data.get("doan") or {}
            game: DoanChuGame = (data or {}).get("game")
            if not game or not game.live:
                return
            cur = game.players[game.current_idx]
            user = await context.bot.get_chat(cur)
            await context.bot.send_message(chat_id, f"{mention(user)} hết giờ, bị loại!", parse_mode="Markdown")
            game.players.pop(game.current_idx)
            if not game.players:
                await context.bot.send_message(chat_id, "🏁 Hết người chơi. Kết thúc ván.")
                game.live = False
                return
            if game.current_idx >= len(game.players):
                game.current_idx = 0
            await context.bot.send_message(chat_id, f"🔔 Tới lượt {mention(await context.bot.get_chat(game.players[game.current_idx]))}",
                                           parse_mode="Markdown")
            await schedule_turn_timeout(context, chat_id, "doan")

    async def on_text_doi(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        data = context.chat_data.get("doi") or {}
        game: DoiChuGame = (data or {}).get("game")
        if not game or not game.live:
            return  # bỏ qua khi không trong ván đối chữ
        text = (update.message.text or "").strip()
        # kiểm tra đúng lượt
        cur = game.players[game.current_idx]
        if cur != update.effective_user.id:
            return
        # luật: 2 từ, phải khớp từ đầu với từ cuối trước
        norm = VietDict.normalize_phrase(text)
        if len(norm.split()) != 2:
            await update.message.reply_text("❌ Cần *2 từ* có nghĩa.", parse_mode="Markdown")
            return
        if game.last_phrase:
            must = game.last_phrase.split()[-1]
            if norm.split()[0].lower() != must.lower():
                await update.message.reply_text(f"❌ Sai một ly, đi *{must} …* mới đúng.", parse_mode="Markdown")
                # loại
                await eliminate_player_doi(update, context, game, reason="sai luật")
                return
        # kiểm tra nghĩa
        if not await vdict.is_valid(norm):
            await update.message.reply_text(f"❌ Cụm không có nghĩa (không tìm thấy). {random.choice(TAUNT_WRONG)}")
            await eliminate_player_doi(update, context, game, reason="không có nghĩa")
            return
        # hợp lệ → cập nhật, chuyển lượt
        game.last_phrase = norm
        game.current_idx = (game.current_idx + 1) % len(game.players)
        await update.message.reply_text(f"✅ Hợp lệ! {_turn_hint(game.last_phrase)}", parse_mode="Markdown")
        await schedule_turn_timeout(context, chat_id, "doi")

    async def eliminate_player_doi(update: Update, context: ContextTypes.DEFAULT_TYPE, game: DoiChuGame, reason: str):
        chat_id = update.effective_chat.id
        game.players.pop(game.current_idx)
        if not game.players or all(p==0 for p in game.players):
            await update.message.reply_text("🏁 Hết người chơi. Kết thúc ván.")
            game.live = False
            return
        if game.current_idx >= len(game.players):
            game.current_idx = 0
        await context.bot.send_message(chat_id, f"🪓 {mention(update.effective_user)} bị loại ({reason}).",
                                       parse_mode="Markdown")
        await context.bot.send_message(chat_id, _turn_hint(game.last_phrase), parse_mode="Markdown")
        await schedule_turn_timeout(context, chat_id, "doi")

    # ====== ĐOÁN CHỮ ======
    async def new_doan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        data: Dict = context.chat_data.setdefault("doan", {})
        game: DoanChuGame = data.setdefault("game", DoanChuGame())
        game.lobby = {update.effective_user.id}
        await chat.send_message("🧩 Mở sảnh đoán chữ! /join để tham gia. 🔔 Tự bắt đầu sau 60s.")
        context.job_queue.run_once(lambda c: asyncio.create_task(begin_doan(update, context)), 60, name=f"lobby_doan_{chat.id}")

    async def begin_doan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        data: Dict = context.chat_data.setdefault("doan", {})
        game: DoanChuGame = data.setdefault("game", DoanChuGame())
        if game.live:
            return
        players = list(game.lobby)
        if not players:
            await chat.send_message("⛔ Không có người tham gia. Huỷ ván.")
            return
        game.live = True
        random.shuffle(players)
        game.players = players
        game.guesses_left = {pid:3 for pid in players}
        game.current_idx = 0
        game.question = gbank.random()
        if not game.question:
            await chat.send_message("📭 Ngân hàng câu hỏi trống. Thêm bằng /addqa.")
            game.live = False
            return
        await chat.send_message(f"🎯 Câu hỏi:\n*{game.question['question']}*", parse_mode="Markdown")
        await chat.send_message(f"🔔 Tới lượt {mention(await context.bot.get_chat(game.players[0]))}", parse_mode="Markdown")
        await schedule_turn_timeout(context, chat.id, "doan")

    async def on_text_doan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        data = context.chat_data.get("doan") or {}
        game: DoanChuGame = (data or {}).get("game")
        if not game or not game.live:
            return
        # kiểm tra đúng lượt
        cur = game.players[game.current_idx]
        if cur != update.effective_user.id:
            return
        guess = (update.message.text or "").strip()
        ans = (game.question or {}).get("answer", "")
        if not ans:
            return
        if guess.lower() == ans.lower():
            await update.message.reply_text(f"🏆 Chính xác! {mention(update.effective_user)} chiến thắng!",
                                            parse_mode="Markdown")
            game.live = False
            return
        # sai
        game.guesses_left[cur] -= 1
        if game.guesses_left[cur] <= 0:
            await update.message.reply_text(f"❌ Sai. {random.choice(TAUNT_WRONG)}\nBạn *hết lượt*, bị loại.",
                                            parse_mode="Markdown")
            game.players.pop(game.current_idx)
            if not game.players:
                await update.message.reply_text("🏁 Không ai đoán đúng. Kết thúc ván.")
                game.live = False
                return
            if game.current_idx >= len(game.players):
                game.current_idx = 0
        else:
            await update.message.reply_text(
                f"❌ Sai. {random.choice(TAUNT_WRONG)}\n"
                f"👉 Còn *{game.guesses_left[cur]}* lượt cho bạn.",
                parse_mode="Markdown"
            )
            game.current_idx = (game.current_idx + 1) % len(game.players)
        await context.bot.send_message(chat_id,
            f"🔔 Tới lượt {mention(await context.bot.get_chat(game.players[game.current_idx]))}",
            parse_mode="Markdown")
        await schedule_turn_timeout(context, chat_id, "doan")

    # ----- Thêm câu hỏi vào Gist:  /addqa CÂU HỎI | ĐÁP ÁN | gợi ý1;gợi ý2;...
    async def addqa(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not (GIST_ID and GIST_TOKEN):
            await update.message.reply_text("Chưa cấu hình GIST_ID/GIST_TOKEN.")
            return
        text = update.message.text or ""
        try:
            _, payload = text.split(" ", 1)
            q, a, hints = [x.strip() for x in payload.split("|", 2)]
            hint_list = [h.strip() for h in hints.split(";") if h.strip()]
        except Exception:
            await update.message.reply_text("Cú pháp: /addqa CÂU HỎI | ĐÁP ÁN | gợi ý1;gợi ý2;...")
            return
        await gbank.add_item(q, a, hint_list)
        await update.message.reply_text("✅ Đã thêm vào ngân hàng câu hỏi.")

    # ----- Bắt đầu ngay (skip sảnh)
    async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if "doi" in context.chat_data and context.chat_data["doi"].get("game"):
            await begin_doi(update, context)
        elif "doan" in context.chat_data and context.chat_data["doan"].get("game"):
            await begin_doan(update, context)
        else:
            await update.message.reply_text("Chưa có sảnh nào. /new_doi hoặc /new_doan trước nhé.")

    # ----- Gắn handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu:"))
    # Đối chữ
    app.add_handler(CommandHandler("new_doi", new_doi))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_doi), group=10)
    # Đoán chữ
    app.add_handler(CommandHandler("new_doan", new_doan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_doan), group=11)
    app.add_handler(CommandHandler("addqa", addqa))
