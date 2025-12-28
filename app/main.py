from __future__ import annotations

import time

from fastapi import FastAPI, Request

from app.config import S, logger
from app.health_routes import router as health_router
from app.memory_legacy import memory_init
from app.memory_routes import router as memory_router
from app.openai_routes import router as openai_router
from app.tools_bus import router as tools_router
from app import memory_v2


app = FastAPI(title="Local AI Gateway", version="0.1")


@app.middleware("http")
async def log_requests(req: Request, call_next):
    start = time.time()
    resp = None
    try:
        resp = await call_next(req)
        return resp
    finally:
        dur_ms = (time.time() - start) * 1000.0
        status = resp.status_code if resp is not None else 500
        logger.info("%s %s -> %d (%.1fms)", req.method, req.url.path, status, dur_ms)


# One-time DB init
memory_init()
if S.MEMORY_V2_ENABLED:
    memory_v2.init(S.MEMORY_DB_PATH)


# Routers
app.include_router(health_router)
app.include_router(openai_router)
app.include_router(memory_router)
app.include_router(tools_router)
