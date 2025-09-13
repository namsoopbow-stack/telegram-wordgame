# webhook.py
import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from bot import register_handlers  # chỉ import đăng ký handlers

BOT_TOKEN   = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not BOT_TOKEN or not WEBHOOK_URL:
    raise SystemExit("Missing BOT_TOKEN or WEBHOOK_URL env")

# Telegram Application
ptb = ApplicationBuilder().token(BOT_TOKEN).build()
register_handlers(ptb)  # gắn toàn bộ handlers

# FastAPI app
app = FastAPI()

@app.on_event("startup")
async def _startup():
    await ptb.initialize()
    await ptb.bot.set_webhook(f"{WEBHOOK_URL.rstrip('/')}/webhook")

@app.on_event("shutdown")
async def _shutdown():
    await ptb.bot.delete_webhook()
    await ptb.shutdown()
    await ptb.stop()

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, ptb.bot)
    await ptb.process_update(update)
    return {"ok": True}
