from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import S


def require_bearer(req: Request) -> None:
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token != S.GATEWAY_BEARER_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bearer token")
