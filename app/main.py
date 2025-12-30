from __future__ import annotations

import time

from fastapi import FastAPI, Request

import httpx

from app.config import S, logger
from app.health_routes import router as health_router
from app.memory_legacy import memory_init
from app.memory_routes import router as memory_router
from app.openai_routes import router as openai_router
from app.model_aliases import get_aliases
from app.tools_bus import router as tools_router
from app import memory_v2


app = FastAPI(title="Local AI Gateway", version="0.1")


@app.on_event("startup")
async def _startup_check_models() -> None:
    """Non-fatal checks to catch common misconfigurations early."""

    # Warn if Ollama-backed aliases point at model tags that aren't present.
    try:
        aliases = get_aliases()
        wanted = sorted({a.upstream_model for a in aliases.values() if a.backend == "ollama" and a.upstream_model})
        if not wanted:
            return

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            # If Ollama isn't reachable, don't spam logs; this can happen on cold boot.
            if r.status_code != 200:
                logger.info("startup: ollama /api/tags status=%s (skipping model check)", r.status_code)
                return

            payload = r.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            present = set()
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, dict) and isinstance(m.get("name"), str):
                        present.add(m["name"])

        missing = [m for m in wanted if m not in present]
        for m in missing:
            logger.warning("startup: ollama model missing: %s (check model_aliases.json or run 'ollama pull %s')", m, m)
    except Exception as e:
        logger.info("startup: model availability check skipped (%s: %s)", type(e).__name__, e)


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
