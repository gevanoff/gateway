from __future__ import annotations

from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.auth import require_bearer
from app.config import S
from app.metrics import render_prometheus_text


router = APIRouter()


@router.get("/health")
@router.get("/health/", include_in_schema=False)
async def health():
    return {"ok": True}


@router.head("/health", include_in_schema=False)
@router.head("/health/", include_in_schema=False)
async def health_head():
    # Explicit HEAD support avoids 405s for some health checkers.
    return PlainTextResponse("", status_code=200)


@router.get("/health/upstreams")
async def health_upstreams(req: Request):
    require_bearer(req)

    results: Dict[str, Any] = {"ok": True, "upstreams": {}}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            results["upstreams"]["ollama"] = {"ok": True, "status": r.status_code}
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["ollama"] = {"ok": False, "error": str(e)}

        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            results["upstreams"]["mlx"] = {"ok": True, "status": r.status_code}
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["mlx"] = {"ok": False, "error": str(e)}

    return results


@router.get("/metrics")
async def metrics_endpoint(req: Request):
    if not getattr(S, "METRICS_ENABLED", True):
        return PlainTextResponse("", status_code=404)
    require_bearer(req)
    return PlainTextResponse(render_prometheus_text(), media_type="text/plain; version=0.0.4")
