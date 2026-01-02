from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.images_backend import generate_images


router = APIRouter()


@router.post("/v1/images/generations")
async def images_generations(req: Request):
    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")

    n = body.get("n", 1)
    size = body.get("size", "1024x1024")
    model = body.get("model")

    try:
        return await generate_images(
            prompt=prompt,
            size=str(size),
            n=int(n),
            model=str(model) if isinstance(model, str) and model.strip() else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
