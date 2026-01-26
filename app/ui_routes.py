from __future__ import annotations

import base64
import io
import hashlib
import ipaddress
import json
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
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse

from app.backends import check_capability, get_admission_controller, get_registry
from app.config import S
from app.health_checker import check_backend_ready
from app.model_aliases import get_aliases
from app.models import ChatCompletionRequest, ChatMessage
from app.openai_utils import now_unix, sse, sse_done
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_mlx_openai, call_ollama, stream_mlx_openai_chat, stream_ollama_chat_as_openai
from app.images_backend import generate_images
from app.tts_backend import generate_tts
from app import ui_conversations
from app import user_store


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


def _session_cookie_name() -> str:
    return (getattr(S, "USER_SESSION_COOKIE", "") or "gateway_session").strip() or "gateway_session"


def _session_token_from_req(req: Request) -> str:
    try:
        token = (req.headers.get("authorization") or "").strip()
        if token.lower().startswith("bearer "):
            return token.split(" ", 1)[1].strip()
    except Exception:
        token = ""

    try:
        token = (req.headers.get("x-session-token") or "").strip()
        if token:
            return token
    except Exception:
        token = ""

    try:
        cookie_name = _session_cookie_name()
        return (req.cookies or {}).get(cookie_name) or ""
    except Exception:
        return ""


def _require_user(req: Request) -> Optional[user_store.User]:
    if not getattr(S, "USER_AUTH_ENABLED", True):
        return None
    token = _session_token_from_req(req)
    user = user_store.get_user_by_session(S.USER_DB_PATH, token=token)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        req.state.user = user
    except Exception:
        pass
    return user


def _coerce_tts_body(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        alt = body.get("input")
        if not isinstance(alt, str) or not alt.strip():
            raise HTTPException(status_code=400, detail="text is required")
    return body


def _tts_gateway_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return headers
    backend = meta.get("backend")
    backend_class = meta.get("backend_class")
    latency = meta.get("upstream_latency_ms")
    if backend:
        headers["x-gateway-backend"] = str(backend)
    if backend_class:
        headers["x-gateway-backend-class"] = str(backend_class)
    if latency is not None:
        headers["x-gateway-upstream-latency-ms"] = str(latency)
    return headers


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


def _sniff_mime(raw: bytes) -> str | None:
    # Best-effort sniff based on magic bytes.
    if not raw:
        return None

    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    head = raw[:256].lstrip()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "image/svg+xml"
    return None


def _gateway_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return headers
    backend = meta.get("backend")
    backend_class = meta.get("backend_class")
    latency = meta.get("upstream_latency_ms")
    if backend:
        headers["x-gateway-backend"] = str(backend)
    if backend_class:
        headers["x-gateway-backend-class"] = str(backend_class)
    if latency is not None:
        headers["x-gateway-upstream-latency-ms"] = str(latency)
    return headers


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
        return raw, (mime or _sniff_mime(raw))

    raw2 = base64.b64decode(s.encode("ascii"), validate=False)
    return raw2, _sniff_mime(raw2)


def _save_ui_image(*, b64: str, mime_hint: str) -> tuple[str, str, str]:
    img_dir = _ui_image_dir()
    ttl_sec = _ui_image_ttl_sec()
    max_bytes = _ui_image_max_bytes()
    _ensure_dir(img_dir)
    _cleanup_ui_images(img_dir, ttl_sec=ttl_sec)

    raw, mime_from_data = _decode_image_b64(b64)
    if len(raw) > max_bytes:
        raise ValueError(f"image too large to cache ({len(raw)} bytes > {max_bytes})")

    sha256 = hashlib.sha256(raw).hexdigest()

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

    return f"/ui/images/{name}", mime, sha256


@router.get("/ui", include_in_schema=False)
async def ui(req: Request) -> HTMLResponse:
    """Main UI entrypoint.

    We keep the legacy UI available at /ui1.
    """

    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat2.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/", include_in_schema=False)
async def ui_slash(req: Request) -> HTMLResponse:
    return await ui(req)


@router.get("/ui1", include_in_schema=False)
async def ui1(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui1/", include_in_schema=False)
async def ui1_slash(req: Request) -> HTMLResponse:
    return await ui1(req)


@router.get("/ui2", include_in_schema=False)
async def ui2(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("chat2.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui2/", include_in_schema=False)
async def ui2_slash(req: Request) -> HTMLResponse:
    return await ui2(req)


@router.get("/ui/image", include_in_schema=False)
async def ui_image_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("image.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/music", include_in_schema=False)
async def ui_music_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("music.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/video", include_in_schema=False)
async def ui_video_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("video.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/ui/tts", include_in_schema=False)
async def ui_tts_frontend(req: Request) -> HTMLResponse:
    _require_ui_access(req)
    html_path = Path(__file__).with_name("static").joinpath("tts.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.post("/ui/api/music", include_in_schema=False)
async def ui_api_music(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    # Best-effort: forward to music backend and return its normalized response.
    from app.music_backend import generate_music

    try:
        out = await generate_music(backend_class=getattr(S, "MUSIC_BACKEND_CLASS", "heartmula_music"), body=body)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"music backend failed: {e}")

    return out


@router.post("/ui/api/tts", include_in_schema=False)
async def ui_api_tts(req: Request):
    _require_ui_access(req)
    _require_user(req)
    body = _coerce_tts_body(await req.json())
    backend_class = (getattr(S, "TTS_BACKEND_CLASS", "") or "").strip() or "pocket_tts"

    check_backend_ready(backend_class, route_kind="tts")
    await check_capability(backend_class, "tts")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "tts")
    try:
        result = await generate_tts(backend_class=backend_class, body=body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tts backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "tts")

    headers = _tts_gateway_headers(result.gateway)
    if result.kind == "json":
        payload = result.payload
        if isinstance(payload, dict):
            payload.setdefault("_gateway", {}).update(result.gateway)
        return JSONResponse(payload or {}, headers=headers)

    if result.audio is None:
        raise HTTPException(status_code=502, detail="tts backend returned no audio")
    return StreamingResponse(result.audio, media_type=result.content_type, headers=headers)


@router.post("/ui/api/auth/login", include_in_schema=False)
async def ui_auth_login(req: Request):
    _require_ui_access(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    user = user_store.authenticate(S.USER_DB_PATH, username=username, password=password)
    if user is None:
        raise HTTPException(status_code=403, detail="invalid credentials")
    ttl = int(getattr(S, "USER_SESSION_TTL_SEC", 0) or 0)
    if ttl <= 0:
        ttl = 60 * 60 * 12
    session = user_store.create_session(S.USER_DB_PATH, user_id=user.id, ttl_sec=ttl)
    resp = JSONResponse({"ok": True, "user": {"id": user.id, "username": user.username}})
    resp.set_cookie(
        _session_cookie_name(),
        session.token,
        max_age=ttl,
        httponly=True,
        secure=False,
        samesite="lax",
    )
    return resp


@router.post("/ui/api/auth/logout", include_in_schema=False)
async def ui_auth_logout(req: Request):
    _require_ui_access(req)
    token = _session_token_from_req(req)
    if token:
        user_store.delete_session(S.USER_DB_PATH, token=token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_session_cookie_name())
    return resp


@router.get("/ui/api/auth/me", include_in_schema=False)
async def ui_auth_me(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": {"id": user.id, "username": user.username}}


@router.get("/ui/api/user/settings", include_in_schema=False)
async def ui_user_settings_get(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"settings": user_store.get_settings(S.USER_DB_PATH, user_id=-1)}
    settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id)
    return {"settings": settings}


@router.put("/ui/api/user/settings", include_in_schema=False)
async def ui_user_settings_put(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    user_store.set_settings(S.USER_DB_PATH, user_id=user.id, settings=settings)
    return {"ok": True}


@router.get("/ui/api/conversations", include_in_schema=False)
async def ui_conversation_list(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        return {"conversations": []}
    return {"conversations": user_store.list_conversations(S.USER_DB_PATH, user_id=user.id)}


@router.post("/ui/api/conversations/new", include_in_schema=False)
async def ui_conversation_new(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        convo = ui_conversations.create()
        return {"conversation_id": convo.id}
    convo = user_store.create_conversation(S.USER_DB_PATH, user_id=user.id)
    return {"conversation_id": convo["id"]}


@router.get("/ui/api/conversations/{conversation_id}", include_in_schema=False)
async def ui_conversation_get(req: Request, conversation_id: str) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        convo = ui_conversations.load(conversation_id)
        if convo is None:
            raise HTTPException(status_code=404, detail="not found")
        return convo.to_dict()
    convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="not found")
    return convo


@router.post("/ui/api/conversations/{conversation_id}/append", include_in_schema=False)
async def ui_conversation_append(req: Request, conversation_id: str) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    msg = body.get("message")
    if not isinstance(msg, dict):
        raise HTTPException(status_code=400, detail="message must be an object")
    try:
        if user is None:
            convo = ui_conversations.append_message(conversation_id, msg)
            updated = convo.updated
        else:
            convo = user_store.append_message(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id, msg=msg)
            updated = convo.get("updated")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to append: {type(e).__name__}: {e}")
    return {"ok": True, "updated": updated}


@router.get("/ui/images/{name}", include_in_schema=False)
async def ui_image_file(req: Request, name: str):
    _require_ui_access(req)
    _require_user(req)

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
    _require_user(req)

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
    _require_user(req)
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
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    cc_routed = ChatCompletionRequest(
        model=upstream_model if backend == "mlx" else cc.model,
        messages=cc.messages,
        tools=None,
        tool_choice=None,
        temperature=cc.temperature,
        max_tokens=cc.max_tokens,
        stream=False,
    )

    try:
        resp = await (call_mlx_openai(cc_routed) if backend == "mlx" else call_ollama(cc, upstream_model))
    finally:
        admission.release(backend_class, "chat")

    # Include routing metadata so the UI can display it.
    if isinstance(resp, dict):
        resp.setdefault("_gateway", {})
        if isinstance(resp.get("_gateway"), dict):
            resp["_gateway"].update({"backend": backend, "model": upstream_model, "reason": route.reason})
    return resp


def _coerce_messages(body: dict[str, Any]) -> list[ChatMessage]:
    raw = body.get("messages")
    if isinstance(raw, list) and raw:
        out: list[ChatMessage] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip() or "user"
            content = item.get("content")
            if not isinstance(content, str):
                content = ""
            out.append(ChatMessage(role=role, content=content))
        if out:
            return out

    # Back-compat: single message.
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return [ChatMessage(role="user", content=message.strip())]
    return []


def _conversation_to_chat_messages(convo: ui_conversations.Conversation) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    if convo.summary:
        msgs.append(ChatMessage(role="system", content=f"Conversation summary:\n{convo.summary.strip()}"))

    for item in convo.messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "user"
        # Skip non-text assistant artifacts from the prompt context.
        if str(item.get("type") or "") == "image":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        msgs.append(ChatMessage(role=role, content=content))

    return msgs


def _conversation_payload_to_chat_messages(convo: Dict[str, Any]) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    summary = str(convo.get("summary") or "").strip()
    if summary:
        msgs.append(ChatMessage(role="system", content=f"Conversation summary:\n{summary}"))

    raw_messages = convo.get("messages")
    if not isinstance(raw_messages, list):
        return msgs

    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "user"
        if str(item.get("type") or "") == "image":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        msgs.append(ChatMessage(role=role, content=content))

    return msgs


def _summary_trigger_bytes() -> int:
    try:
        return int(getattr(S, "UI_CHAT_SUMMARY_TRIGGER_BYTES", 0) or 0)
    except Exception:
        return 0


def _summary_keep_last_messages() -> int:
    try:
        return int(getattr(S, "UI_CHAT_SUMMARY_KEEP_LAST_MESSAGES", 12) or 12)
    except Exception:
        return 12


async def _summarize_if_needed(convo: ui_conversations.Conversation) -> ui_conversations.Conversation:
    trigger = _summary_trigger_bytes()
    if trigger <= 0:
        return convo

    # Estimate size based on current stored JSON-ish payload.
    try:
        approx = len(json.dumps(convo.to_dict(), ensure_ascii=False).encode("utf-8"))
    except Exception:
        approx = 0
    if approx <= trigger:
        return convo

    keep_n = max(4, _summary_keep_last_messages())
    tail = convo.messages[-keep_n:]
    head = convo.messages[:-keep_n]
    if not head:
        return convo

    head_text_parts: list[str] = []
    for m in head:
        if not isinstance(m, dict):
            continue
        if str(m.get("type") or "") == "image":
            continue
        role = str(m.get("role") or "").strip() or "user"
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        head_text_parts.append(f"{role}: {content.strip()}")

    if not head_text_parts:
        convo.messages = tail
        convo.updated = int(time.time())
        ui_conversations.save(convo)
        return convo

    summarizer_model = "long"  # prefer long-context alias if present
    summary_prompt = (
        "Summarize the conversation so far for future context. "
        "Preserve user preferences, goals, key facts, constraints, decisions, and open questions. "
        "Do not include private reasoning or chain-of-thought. Output concise bullet points.\n\n"
        + "\n".join(head_text_parts)
    )

    cc_sum = ChatCompletionRequest(
        model=summarizer_model,
        messages=[ChatMessage(role="user", content=summary_prompt)],
        stream=False,
    )

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc_sum.model,
        headers={},
        messages=[m.model_dump(exclude_none=True) for m in cc_sum.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")
    cc_sum_routed = ChatCompletionRequest(
        model=upstream_model if backend == "mlx" else cc_sum.model,
        messages=cc_sum.messages,
        stream=False,
    )
    try:
        resp = await (call_mlx_openai(cc_sum_routed) if backend == "mlx" else call_ollama(cc_sum, upstream_model))
    finally:
        admission.release(backend_class, "chat")
    text = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    if not isinstance(text, str):
        text = ""

    prior = (convo.summary or "").strip()
    merged = (prior + "\n" + text.strip()).strip() if prior and text.strip() else (text.strip() or prior)
    convo.summary = merged
    convo.messages = tail
    convo.updated = int(time.time())
    ui_conversations.save(convo)
    return convo


@router.post("/ui/api/chat_stream", include_in_schema=False)
async def ui_chat_stream(req: Request):
    """Tokenless SSE stream for the browser UI.

    Emits gateway status events (routing/backend/model), optional thinking snippets,
    and then streamed text deltas. This intentionally does NOT expose hidden
    chain-of-thought; it only streams assistant-visible text and gateway metadata.
    """

    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    model = (body.get("model") or "fast").strip()
    conversation_id = str(body.get("conversation_id") or "").strip()
    message_text = body.get("message")

    # Prefer server-side conversation history if a conversation_id is provided.
    if conversation_id:
        if user is None:
            convo = ui_conversations.load(conversation_id)
            if convo is None:
                raise HTTPException(status_code=404, detail="conversation not found")

            if isinstance(message_text, str) and message_text.strip():
                try:
                    ui_conversations.append_message(conversation_id, {"role": "user", "content": message_text.strip()})
                    convo = ui_conversations.load(conversation_id) or convo
                except Exception:
                    pass

            # Best-effort summarization/pruning.
            try:
                convo = await _summarize_if_needed(convo)
            except Exception:
                pass

            messages = _conversation_to_chat_messages(convo)
        else:
            convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id)
            if convo is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            if isinstance(message_text, str) and message_text.strip():
                try:
                    user_store.append_message(
                        S.USER_DB_PATH,
                        user_id=user.id,
                        conversation_id=conversation_id,
                        msg={"role": "user", "content": message_text.strip()},
                    )
                    convo = user_store.get_conversation(S.USER_DB_PATH, user_id=user.id, conversation_id=conversation_id) or convo
                except Exception:
                    pass
            messages = _conversation_payload_to_chat_messages(convo)
    else:
        messages = _coerce_messages(body)

    if not messages:
        raise HTTPException(status_code=400, detail="messages required")

    cc = ChatCompletionRequest(
        model=model,
        messages=messages,
        stream=True,
    )

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={k.lower(): v for k, v in req.headers.items()},
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )

    backend: Literal["ollama", "mlx"] = route.backend
    upstream_model = route.model

    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    try:
        cc_routed = ChatCompletionRequest(
            model=upstream_model if backend == "mlx" else cc.model,
            messages=cc.messages,
            tools=None,
            tool_choice=None,
            temperature=cc.temperature,
            max_tokens=cc.max_tokens,
            stream=True,
        )

        if backend == "mlx":
            payload = cc_routed.model_dump(exclude_none=True)
            payload["model"] = upstream_model
            payload["stream"] = True
            upstream_gen = stream_mlx_openai_chat(payload)
        else:
            upstream_gen = stream_ollama_chat_as_openai(cc_routed, upstream_model)
    except Exception:
        admission.release(backend_class, "chat")
        raise

    async def gen():
        try:
            # Announce routing info first
            yield sse({"type": "route", "backend": backend, "model": upstream_model, "reason": route.reason})

            full_text = ""

            async for chunk in upstream_gen:
                for line in chunk.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    data = line[len(b"data:") :].strip()
                    if data == b"[DONE]":
                        yield sse({"type": "done"})
                        yield sse_done()
                        return

                    try:
                        j = json.loads(data)
                    except Exception:
                        continue

                    if isinstance(j, dict) and isinstance(j.get("error"), dict):
                        yield sse({"type": "error", "error": j.get("error")})
                        continue

                    try:
                        delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                        text = delta.get("content")
                        thinking = delta.get("thinking")
                    except Exception:
                        text = None
                        thinking = None

                    if isinstance(thinking, str) and thinking:
                        yield sse({"type": "thinking", "thinking": thinking})

                    if isinstance(text, str) and text:
                        full_text += text
                        yield sse({"type": "delta", "delta": text})

            # After streaming completes, persist assistant message (if any)
            if conversation_id:
                try:
                    if user is None:
                        ui_conversations.append_message(
                            conversation_id,
                            {
                                "role": "assistant",
                                "content": full_text,
                                "backend": backend,
                                "model": upstream_model,
                                "reason": route.reason,
                            },
                        )
                    else:
                        user_store.append_message(
                            S.USER_DB_PATH,
                            user_id=user.id,
                            conversation_id=conversation_id,
                            msg={
                                "role": "assistant",
                                "content": full_text,
                                "backend": backend,
                                "model": upstream_model,
                                "reason": route.reason,
                            },
                        )
                except Exception:
                    # Best-effort persistence; do not fail the stream on storage errors.
                    pass

            # Signal completion to the UI
            yield sse({"type": "done"})
            yield sse_done()
        finally:
            admission.release(backend_class, "chat")

    out = StreamingResponse(gen(), media_type="text/event-stream")
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = upstream_model
    out.headers["X-Router-Reason"] = route.reason
    return out


@router.post("/ui/api/image", include_in_schema=False)
async def ui_image(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    _require_user(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")

    size = str(body.get("size") or "1024x1024")
    n = int(body.get("n") or 1)
    model = body.get("model")

    options = {}
    for k in [
        "seed",
        "steps",
        "num_inference_steps",
        "guidance",
        "guidance_scale",
        "cfg_scale",
        "negative_prompt",
        "sampler",
        "scheduler",
        "style",
        "quality",
    ]:
        if k in body:
            options[k] = body.get(k)
    if not options:
        options = None

    try:
        resp = await generate_images(
            prompt=prompt,
            size=size,
            n=n,
            model=str(model) if isinstance(model, str) and model.strip() else None,
            options=options,
        )

        # Prefer short-lived URLs for the browser (avoids huge data: URIs and broken rendering).
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            gw = resp.get("_gateway") if isinstance(resp.get("_gateway"), dict) else {}
            mime = (gw.get("mime") or "image/png") if isinstance(gw, dict) else "image/png"
            ttl_sec = _ui_image_ttl_sec()

            out_items: list[dict[str, Any]] = []
            first_sha256: str | None = None
            first_mime: str | None = None
            for item in resp.get("data")[:n]:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json")
                if isinstance(b64, str) and b64.strip():
                    url, mime_used, sha256 = _save_ui_image(b64=b64, mime_hint=str(mime))
                    out_items.append({"url": url})
                    mime = mime_used
                    if first_sha256 is None:
                        first_sha256 = sha256
                        first_mime = mime_used

            if out_items:
                resp["data"] = out_items
                resp.setdefault("_gateway", {})
                if isinstance(resp.get("_gateway"), dict):
                    resp["_gateway"].update({"mime": mime, "ui_cache": True, "ttl_sec": ttl_sec})
                    if first_sha256:
                        resp["_gateway"].update({"ui_image_sha256": first_sha256})
                    if first_mime:
                        resp["_gateway"].update({"ui_image_mime": first_mime})

        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
