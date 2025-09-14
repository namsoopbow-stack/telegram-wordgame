# webhook.py
import os
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from bot import build_app, initialize, stop

app = FastAPI(title="WordGameBot")
telegram_app = build_app()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret123")
SECRET_PATH    = os.environ.get("SECRET_PATH", "hook")

@app.on_event("startup")
async def _startup():
    await initialize(telegram_app)

@app.on_event("shutdown")
async def _shutdown():
    await stop(telegram_app)

@app.get("/")
async def home():
    return {"ok": True, "name": "wordgame-bot"}

@app.post(f"/{SECRET_PATH}/{WEBHOOK_SECRET}")
async def handle(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
