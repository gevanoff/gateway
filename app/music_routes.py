from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.backends import check_capability, get_admission_controller
from app.config import S
from app.health_checker import check_backend_ready
from app.music_backend import generate_music


router = APIRouter()


@router.post("/v1/music/generations")
async def music_generations(req: Request):
    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    # Minimal validation: require some text prompt.
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        # Allow OpenAI-ish callers to use "input".
        alt = body.get("input")
        if not isinstance(alt, str) or not alt.strip():
            raise HTTPException(status_code=400, detail="prompt must be a non-empty string")

    backend_class = (getattr(S, "MUSIC_BACKEND_CLASS", "") or "").strip() or "heartmula_music"

    # Backend health/readiness + capability.
    check_backend_ready(backend_class)
    await check_capability(backend_class, "music")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "music")
    try:
        return await generate_music(backend_class=backend_class, body=body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"music backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "music")
