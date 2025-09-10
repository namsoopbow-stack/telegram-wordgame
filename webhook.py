# webhook.py
from fastapi import FastAPI, Request
from bot import get_app

app = FastAPI()
tg_app = get_app()

# Khởi động/ tắt Application để có job_queue, timers, v.v.
@app.on_event("startup")
async def _startup():
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def _shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    # Đẩy update vào hàng đợi của PTB
    await tg_app.update_queue.put(data)
    return {"ok": True}
