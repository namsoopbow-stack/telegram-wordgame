# webhook.py
from fastapi import FastAPI, Request
from telegram import Update
from bot import build_app, initialize, stop

app = FastAPI(title="Multi-Game Telegram Bot")
tg_app = build_app()

@app.on_event("startup")
async def _startup():
    await initialize(tg_app)
    await tg_app.start()

@app.on_event("shutdown")
async def _shutdown():
    await tg_app.stop()
    await stop(tg_app)

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
