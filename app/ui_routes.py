from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
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


@router.get("/ui", include_in_schema=False)
async def ui(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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

    try:
        return await generate_images(prompt=prompt, size=size, n=n)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
