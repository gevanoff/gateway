from __future__ import annotations

from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request

from app.auth import require_bearer
from app.config import S


router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


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
