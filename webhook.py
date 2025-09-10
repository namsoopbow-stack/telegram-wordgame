# webhook.py
import os
from fastapi import FastAPI, Request
from telegram import Update

from bot import build_application

app = FastAPI()
tg_app = build_application()

@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    await tg_app.start()
    # (tuỳ bạn đã set webhook với BotFather hay không)
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL")
    if base:
        await tg_app.bot.set_webhook(f"{base}/telegram/webhook")

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
