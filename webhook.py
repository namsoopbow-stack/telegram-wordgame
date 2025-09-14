# webhook.py (bản vá không-crash)
import os
import logging
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from telegram import Update
from telegram.ext import Application

from bot import build_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wordgame.webhook")

app = FastAPI(title="wordgame-bot")

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret-path")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

tg_app: Application | None = None

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "wordgame-bot OK"

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    # báo trạng thái PTB (để bạn dễ kiểm tra)
    status = "ready" if tg_app else "not-ready"
    return f"ok ({status})"

@app.on_event("startup")
async def on_startup():
    """
    KHÔNG bao giờ để exception rơi ra ngoài -> uvicorn exit 1.
    Bất cứ lỗi gì cũng log + tiếp tục chạy để healthcheck vẫn ok.
    """
    global tg_app
    try:
        tg_app = build_application(BOT_TOKEN)
        if tg_app is None:
            log.warning("PTB Application chưa khởi tạo do thiếu TELEGRAM_TOKEN.")
            return

        try:
            await tg_app.initialize()
            await tg_app.start()
            log.info("PTB Application started.")
        except Exception as e:
            log.exception("PTB start error (Application): %s", e)
            tg_app = None  # tránh dùng app hỏng

        if PUBLIC_BASE_URL and tg_app:
            url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhook/{WEBHOOK_SECRET}"
            try:
                await tg_app.bot.set_webhook(url=url, allowed_updates=Update.ALL_TYPES)
                log.info("Webhook set to: %s", url)
            except Exception as e:
                log.exception("Set webhook failed: %s", e)
        elif not PUBLIC_BASE_URL:
            log.warning("PUBLIC_BASE_URL chưa cấu hình — bỏ qua setWebhook.")
    except Exception as e:
        # Chốt chặn cuối: tuyệt đối không để raise ra ngoài
        log.exception("Startup fatal caught (server vẫn chạy): %s", e)

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    try:
        if tg_app:
            try:
                await tg_app.stop()
                await tg_app.shutdown()
                log.info("PTB Application stopped.")
            except Exception as e:
                log.exception("PTB stop error: %s", e)
    except Exception as e:
        log.exception("Shutdown fatal caught: %s", e)

@app.post("/webhook/{secret}")
async def telegram_update(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    if tg_app is None:
        log.warning("Nhận update nhưng PTB chưa sẵn sàng (thiếu/sai TELEGRAM_TOKEN?).")
        return Response(status_code=200)

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    try:
        update = Update.de_json(data=data, bot=tg_app.bot)
        await tg_app.update_queue.put(update)
        return Response(status_code=200)
    except Exception as e:
        log.exception("Process update error: %s", e)
        return Response(status_code=500)
