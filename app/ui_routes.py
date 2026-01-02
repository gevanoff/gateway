from __future__ import annotations

import base64
import ipaddress
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse

from app.config import S
from app.model_aliases import get_aliases
from app.models import ChatCompletionRequest, ChatMessage
from app.openai_utils import now_unix
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_mlx_openai, call_ollama
from app.images_backend import generate_images


router = APIRouter()


_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _client_ip(req: Request) -> str:
    try:
        c = req.client
        return (c.host or "").strip() if c else ""
    except Exception:
        return ""


def _parse_ip_allowlist(raw: str) -> list[Any]:
    items: list[Any] = []
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            # Accept bare IPs and CIDRs.
            if "/" in s:
                items.append(ipaddress.ip_network(s, strict=False))
            else:
                items.append(ipaddress.ip_address(s))
        except Exception:
            continue
    return items


def _require_ui_access(req: Request) -> None:
    raw = (getattr(S, "UI_IP_ALLOWLIST", "") or "").strip()
    if not raw:
        raise HTTPException(status_code=403, detail="UI disabled (set UI_IP_ALLOWLIST to trusted IPs/CIDRs)")

    ip_s = _client_ip(req)
    try:
        ip = ipaddress.ip_address(ip_s)
    except Exception:
        raise HTTPException(status_code=403, detail="UI denied (unknown client IP)")

    allow = _parse_ip_allowlist(raw)
    for item in allow:
        try:
            if isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                if ip == item:
                    return
            else:
                if ip in item:
                    return
        except Exception:
            continue

    raise HTTPException(status_code=403, detail="UI denied (client IP not allowlisted)")


def _ui_image_dir() -> str:
    return (getattr(S, "UI_IMAGE_DIR", "") or "/var/lib/gateway/data/ui_images").strip() or "/var/lib/gateway/data/ui_images"


def _ui_image_ttl_sec() -> int:
    try:
        return int(getattr(S, "UI_IMAGE_TTL_SEC", 900) or 900)
    except Exception:
        return 900


def _ui_image_max_bytes() -> int:
    try:
        return int(getattr(S, "UI_IMAGE_MAX_BYTES", 50_000_000) or 50_000_000)
    except Exception:
        return 50_000_000


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _cleanup_ui_images(path: str, *, ttl_sec: int) -> None:
    # Best-effort cleanup; never fail the request for cleanup errors.
    if ttl_sec <= 0:
        return
    now = time.time()
    cutoff = now - float(ttl_sec)
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                if st.st_mtime < cutoff:
                    os.remove(full)
            except FileNotFoundError:
                continue
            except Exception:
                continue
    except Exception:
        return


def _mime_to_ext(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m == "image/png":
        return "png"
    if m == "image/jpeg":
        return "jpg"
    if m == "image/webp":
        return "webp"
    if m == "image/svg+xml":
        return "svg"
    return "bin"


def _decode_image_b64(b64_or_data_url: str) -> tuple[bytes, str | None]:
    s = (b64_or_data_url or "").strip()
    if not s:
        raise ValueError("empty image data")

    if s.startswith("data:"):
        # data:<mime>;base64,<payload>
        try:
            header, payload = s.split(",", 1)
        except ValueError:
            raise ValueError("invalid data URL")

        mime = None
        try:
            header2 = header[5:]
            parts = header2.split(";")
            if parts:
                mime = parts[0].strip() or None
        except Exception:
            mime = None

        raw = base64.b64decode(payload.encode("ascii"), validate=False)
        return raw, mime

    raw2 = base64.b64decode(s.encode("ascii"), validate=False)
    return raw2, None


def _save_ui_image(*, b64: str, mime_hint: str) -> tuple[str, str]:
    img_dir = _ui_image_dir()
    ttl_sec = _ui_image_ttl_sec()
    max_bytes = _ui_image_max_bytes()
    _ensure_dir(img_dir)
    _cleanup_ui_images(img_dir, ttl_sec=ttl_sec)

    raw, mime_from_data = _decode_image_b64(b64)
    if len(raw) > max_bytes:
        raise ValueError(f"image too large to cache ({len(raw)} bytes > {max_bytes})")

    mime = (mime_from_data or mime_hint or "application/octet-stream").strip()
    ext = _mime_to_ext(mime)
    name = f"{secrets.token_urlsafe(18)}.{ext}"
    # Make the filename deterministic-safe.
    name = name.replace("-", "_")
    if not _SAFE_FILE_RE.match(name):
        # Extremely unlikely, but fail closed.
        raise ValueError("failed to generate safe filename")

    tmp = os.path.join(img_dir, f".{name}.tmp")
    dst = os.path.join(img_dir, name)
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, dst)

    return f"/ui/images/{name}", mime


@router.get("/ui", include_in_schema=False)
async def ui(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/images/{name}", include_in_schema=False)
async def ui_image_file(req: Request, name: str):
    _require_ui_access(req)

    if not _SAFE_FILE_RE.match(name or ""):
        raise HTTPException(status_code=404, detail="not found")

    img_dir = _ui_image_dir()
    ttl_sec = _ui_image_ttl_sec()
    _ensure_dir(img_dir)
    _cleanup_ui_images(img_dir, ttl_sec=ttl_sec)

    full = os.path.join(img_dir, name)
    try:
        st = os.stat(full)
        if ttl_sec > 0 and (time.time() - float(st.st_mtime)) > float(ttl_sec):
            # Expired; best-effort delete.
            try:
                os.remove(full)
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="expired")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    media_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")

    headers = {"cache-control": "private, max-age=60"}
    return FileResponse(full, media_type=media_type, headers=headers)


@router.get("/ui/api/models", include_in_schema=False)
async def ui_models(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)

    now = now_unix()
    data: Dict[str, Any] = {"object": "list", "data": []}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = r.json().get("models", [])
            for m in models:
                name = m.get("name")
                if name:
                    data["data"].append({"id": f"ollama:{name}", "object": "model", "created": now, "owned_by": "local"})
        except Exception:
            pass

        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            models = r.json().get("data", [])
            for m in models:
                mid = m.get("id")
                if mid:
                    data["data"].append({"id": f"mlx:{mid}", "object": "model", "created": now, "owned_by": "local"})
        except Exception:
            pass

    # Add convenience backend pseudo-models.
    data["data"].append({"id": "ollama", "object": "model", "created": now, "owned_by": "gateway"})
    data["data"].append({"id": "mlx", "object": "model", "created": now, "owned_by": "gateway"})

    # Add configured aliases so the UI can select stable names (fast/coder/etc).
    aliases = get_aliases()
    for alias_name in sorted(aliases.keys()):
        a = aliases[alias_name]
        item: Dict[str, Any] = {"id": alias_name, "object": "model", "created": now, "owned_by": "gateway"}
        item["backend"] = a.backend
        item["upstream_model"] = a.upstream_model
        if a.context_window:
            item["context_window"] = a.context_window
        if a.tools is not None:
            item["tools"] = a.tools
        if a.max_tokens_cap is not None:
            item["max_tokens_cap"] = a.max_tokens_cap
        if a.temperature_cap is not None:
            item["temperature_cap"] = a.temperature_cap
        data["data"].append(item)

    return data


@router.post("/ui/api/chat", include_in_schema=False)
async def ui_chat(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    body = await req.json()
    model = (body.get("model") or "fast").strip()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    cc = ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=message)],
        stream=False,
    )

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={k.lower(): v for k, v in req.headers.items()},
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    cc_routed = ChatCompletionRequest(
        model=upstream_model if backend == "mlx" else cc.model,
        messages=cc.messages,
        tools=None,
        tool_choice=None,
        temperature=cc.temperature,
        max_tokens=cc.max_tokens,
        stream=False,
    )

    resp = await (call_mlx_openai(cc_routed) if backend == "mlx" else call_ollama(cc, upstream_model))

    # Include routing metadata so the UI can display it.
    if isinstance(resp, dict):
        resp.setdefault("_gateway", {})
        if isinstance(resp.get("_gateway"), dict):
            resp["_gateway"].update({"backend": backend, "model": upstream_model, "reason": route.reason})
    return resp


@router.post("/ui/api/image", include_in_schema=False)
async def ui_image(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")

    size = str(body.get("size") or "1024x1024")
    n = int(body.get("n") or 1)
    model = body.get("model")

    try:
        resp = await generate_images(
            prompt=prompt,
            size=size,
            n=n,
            model=str(model) if isinstance(model, str) and model.strip() else None,
        )

        # Prefer short-lived URLs for the browser (avoids huge data: URIs and broken rendering).
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            gw = resp.get("_gateway") if isinstance(resp.get("_gateway"), dict) else {}
            mime = (gw.get("mime") or "image/png") if isinstance(gw, dict) else "image/png"
            ttl_sec = _ui_image_ttl_sec()

            out_items: list[dict[str, Any]] = []
            for item in resp.get("data")[:n]:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json")
                if isinstance(b64, str) and b64.strip():
                    url, mime_used = _save_ui_image(b64=b64, mime_hint=str(mime))
                    out_items.append({"url": url})
                    mime = mime_used

            if out_items:
                resp["data"] = out_items
                resp.setdefault("_gateway", {})
                if isinstance(resp.get("_gateway"), dict):
                    resp["_gateway"].update({"mime": mime, "ui_cache": True, "ttl_sec": ttl_sec})

        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
