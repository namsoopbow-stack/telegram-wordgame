# webhook.py
from fastapi import FastAPI, Request
from telegram import Update
from bot import build_application
import traceback

app = FastAPI()
tg_app = build_application()  # tạo Application 1 lần

# Khởi động bot khi server start
@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    await tg_app.start()        # bật JobQueue & các handler

# Tắt bot khi server stop
@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

# Healthcheck
@app.get("/")
async def root():
    return {"status": "ok"}

# Webhook nhận update từ Telegram
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raw = await request.body()
        print("!! cannot parse json, raw body:", raw[:500])
        return {"ok": True}

    try:
        print(">> incoming update:", str(data)[:300])
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        print("!! ERROR processing update:", e)
        traceback.print_exc()
    return {"ok": True}
