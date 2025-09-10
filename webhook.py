import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from bot import application  # dùng Application đã build sẵn

app = FastAPI()

@app.get("/")
async def root():
    return JSONResponse({"status": "ok"})

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return JSONResponse({"ok": True})
