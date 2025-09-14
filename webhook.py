# webhook.py
from bot import build_app, initialize, stop

app = build_app()

@app.on_event("startup")
async def _startup():
    await initialize(app)

@app.on_event("shutdown")
async def _shutdown():
    await stop(app)
