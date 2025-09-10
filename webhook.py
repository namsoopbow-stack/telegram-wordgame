# webhook.py
from fastapi import FastAPI, Request
from telegram import Update
from bot import build_application
import traceback

app = FastAPI()
tg_app = build_application()  # tạo Application đúng 1 lần

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        # Nếu parse JSON fail, log raw body và trả 200 để tránh 500
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
        # vẫn trả 200 để Telegram không lặp lại lỗi 500
        # (nhưng log vẫn cho ta biết lỗi gì)
    return {"ok": True}
