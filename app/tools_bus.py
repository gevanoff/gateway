from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from typing import Any, Dict
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.config import S
from app.models import ToolExecRequest


router = APIRouter()


def tool_shell(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_SHELL:
        return {"ok": False, "error": "shell tool disabled"}

    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "error": "cmd must be a non-empty string"}

    cwd = S.TOOLS_SHELL_CWD
    os.makedirs(cwd, exist_ok=True)

    try:
        parts = shlex.split(cmd)
        cp = subprocess.run(
            parts,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=S.TOOLS_SHELL_TIMEOUT_SEC,
            check=False,
        )
        return {
            "ok": True,
            "returncode": cp.returncode,
            "stdout": cp.stdout[-20000:],
            "stderr": cp.stderr[-20000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {S.TOOLS_SHELL_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"ok": True, "content": f.read()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_http_fetch(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_HTTP_FETCH:
        return {"ok": False, "error": "http_fetch tool disabled"}

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url must be a non-empty string"}

    method = (args.get("method") or "GET").strip().upper()
    if method != "GET":
        return {"ok": False, "error": "only GET is supported"}

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "error": "only http/https URLs are allowed"}

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return {"ok": False, "error": "url must include a hostname"}

    allowed_hosts = {h.strip().lower() for h in (S.TOOLS_HTTP_ALLOWED_HOSTS or "").split(",") if h.strip()}
    if host not in allowed_hosts:
        return {"ok": False, "error": f"host not allowed: {host}"}

    hdrs = args.get("headers")
    if hdrs is None:
        headers = {}
    elif isinstance(hdrs, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in hdrs.items()):
        headers = hdrs
    else:
        return {"ok": False, "error": "headers must be an object of string:string"}

    max_bytes = int(S.TOOLS_HTTP_MAX_BYTES)
    timeout = float(S.TOOLS_HTTP_TIMEOUT_SEC)

    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("GET", url, headers=headers) as r:
                status = r.status_code
                out = bytearray()
                for chunk in r.iter_bytes():
                    if not chunk:
                        continue
                    remaining = max_bytes - len(out)
                    if remaining <= 0:
                        break
                    out.extend(chunk[:remaining])
                content_type = r.headers.get("content-type", "")

        body_text = None
        try:
            body_text = out.decode("utf-8")
        except Exception:
            body_text = None

        return {
            "ok": True,
            "status": status,
            "content_type": content_type,
            "truncated": len(out) >= max_bytes,
            "body_text": body_text,
            "body_base64": None if body_text is not None else base64.b64encode(bytes(out)).decode("ascii"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


TOOL_IMPL = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "http_fetch": tool_http_fetch,
}


def _allowed_tool_names() -> set[str]:
    raw = (S.TOOLS_ALLOWLIST or "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}

    allowed: set[str] = set()
    if S.TOOLS_ALLOW_SHELL:
        allowed.add("shell")
    if S.TOOLS_ALLOW_FS:
        allowed.update({"read_file", "write_file"})
    if S.TOOLS_ALLOW_HTTP_FETCH:
        allowed.add("http_fetch")
    return allowed


def is_tool_allowed(name: str) -> bool:
    return name in _allowed_tool_names()


TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "shell": {
        "name": "shell",
        "description": "Run a command locally (no shell=True).",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "Command string to execute."}},
            "required": ["cmd"],
            "additionalProperties": False,
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Read a local text file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write a local text file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "http_fetch": {
        "name": "http_fetch",
        "description": "Fetch a URL via GET with host allowlist and size limits.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def run_tool_call(name: str, arguments_json: str) -> Dict[str, Any]:
    fn = TOOL_IMPL.get(name)
    if not fn:
        return {"ok": False, "error": f"unknown tool: {name}"}
    if not is_tool_allowed(name):
        return {"ok": False, "error": f"tool not allowed: {name}"}
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception:
        return {"ok": False, "error": "tool arguments must be valid JSON"}
    return fn(args)


@router.get("/v1/tools")
async def v1_tools_list(req: Request):
    require_bearer(req)
    allowed = sorted(_allowed_tool_names())
    data = []
    for name in allowed:
        sch = TOOL_SCHEMAS.get(name)
        if sch:
            data.append({"name": sch["name"], "description": sch["description"], "parameters": sch["parameters"]})
        else:
            data.append({"name": name, "description": "(no schema)", "parameters": {"type": "object"}})
    return {"object": "list", "data": data}


@router.post("/v1/tools/{name}")
async def v1_tools_exec(req: Request, name: str):
    require_bearer(req)
    if name not in TOOL_IMPL:
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    if not is_tool_allowed(name):
        raise HTTPException(status_code=403, detail=f"tool not allowed: {name}")
    body = await req.json()
    tr = ToolExecRequest(**body)
    return TOOL_IMPL[name](tr.arguments)
