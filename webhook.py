import os, hmac, hashlib
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application
from bot import build_application

app = FastAPI()
_application = None

async def get_app():
    global _application
    if _application is None:
        token = os.environ["TELEGRAM_TOKEN"]
        _application = await build_application(token)
    return _application

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    appTG = await get_app()
    update = Update.de_json(data=(await request.json()), bot=appTG.bot)
    await appTG.update_queue.put(update)
    return {"ok": True}

@app.get("/")
async def health():
    return {"status": "ok"}
