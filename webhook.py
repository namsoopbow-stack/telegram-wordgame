# webhook.py
from fastapi import FastAPI, Request
from telegram import Update
from bot import build_application

app = FastAPI()
tg_app = build_application()  # tạo 1 lần

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    print(">> incoming update:", str(data)[:300])  # log ngắn để debug
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
