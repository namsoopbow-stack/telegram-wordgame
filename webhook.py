# webhook.py
from fastapi import FastAPI, Request
from telegram import Update
from bot import build_app   # bot.py phải có hàm build_app() trả về Application (PTB v21+)

app = FastAPI(title="Telegram Webhook")
tg_app = build_app()

# ====== FastAPI lifecycle ======
@app.on_event("startup")
async def _startup():
    # Khởi tạo & bật PTB Application
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def _shutdown():
    # Tắt PTB Application gọn gàng
    await tg_app.stop()
    await tg_app.shutdown()

# ====== Healthcheck ======
@app.get("/")
async def root():
    # Dùng Render/uptime robot ping kiểm tra
    return {"status": "ok"}

# ====== Telegram webhook endpoint ======
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram sẽ POST JSON update vào đây.
    Ta chuyển JSON -> Update rồi cho PTB xử lý.
    """
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)  # PTB v21.x
    await tg_app.process_update(update)
    return {"ok": True}
