from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter()


@router.get("/ui", include_in_schema=False)
async def ui() -> HTMLResponse:
    # Intentionally not bearer-protected: the page itself is harmless and
    # callers still need a valid bearer token to use the API.
    html_path = Path(__file__).with_name("static").joinpath("chat.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
