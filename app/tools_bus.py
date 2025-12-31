from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time
from typing import Any, Dict
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.config import S
from app.models import ToolExecRequest
from app.openai_utils import new_id, now_unix


router = APIRouter()


def _tools_log_path() -> str:
    # Configurable via Settings; default stays within /var/lib/gateway.
    return (S.TOOLS_LOG_PATH or "/var/lib/gateway/data/tools_bus.jsonl").strip()


def _truncate(s: Any, *, max_chars: int) -> Any:
    if isinstance(s, str) and len(s) > max_chars:
        return s[:max_chars] + "…"
    return s


def _safe_json(obj: Any, *, max_chars: int = 20_000) -> str:
    try:
        return _truncate(json.dumps(obj, separators=(",", ":"), sort_keys=True), max_chars=max_chars)  # type: ignore[return-value]
    except Exception:
        return "{}"


def _log_tool_event(event: Dict[str, Any]) -> None:
    path = _tools_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Tool execution should not fail just because logging failed.
        return


def _validate_against_schema(params_schema: Dict[str, Any], args: Any) -> list[str]:
    """Minimal validation for our tool parameter schemas.

    Supports:
    - object schemas with properties/required/additionalProperties
    - string
    - array of strings
    """

    errs: list[str] = []
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]

    if (params_schema.get("type") or "") != "object":
        return []

    props = params_schema.get("properties")
    if not isinstance(props, dict):
        props = {}

    required = params_schema.get("required")
    if isinstance(required, list):
        for k in required:
            if isinstance(k, str) and k not in args:
                errs.append(f"missing required field: {k}")

    additional = params_schema.get("additionalProperties")
    if additional is False:
        allowed = set(k for k in props.keys() if isinstance(k, str))
        extra = sorted([k for k in args.keys() if k not in allowed])
        for k in extra:
            errs.append(f"unexpected field: {k}")

    for key, sch in props.items():
        if not isinstance(key, str) or key not in args:
            continue
        v = args.get(key)
        if not isinstance(sch, dict):
            continue
        t = sch.get("type")
        if t == "string":
            if not isinstance(v, str):
                errs.append(f"{key} must be a string")
        elif t == "array":
            items = sch.get("items")
            if not isinstance(v, list):
                errs.append(f"{key} must be an array")
            else:
                if isinstance(items, dict) and items.get("type") == "string":
                    if not all(isinstance(x, str) for x in v):
                        errs.append(f"{key} items must be strings")
        elif t == "object":
            if not isinstance(v, dict):
                errs.append(f"{key} must be an object")

    return errs


def tool_shell(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_SHELL:
        return {"ok": False, "error": "shell tool disabled"}

    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "error": "cmd must be a non-empty string"}

    cwd = S.TOOLS_SHELL_CWD
    os.makedirs(cwd, exist_ok=True)

    allowed = {p.strip() for p in (S.TOOLS_SHELL_ALLOWED_CMDS or "").split(",") if p.strip()}
    if not allowed:
        return {"ok": False, "error": "shell tool not configured (TOOLS_SHELL_ALLOWED_CMDS empty)"}

    try:
        parts = shlex.split(cmd)
        if not parts:
            return {"ok": False, "error": "cmd must be a non-empty string"}
        exe = parts[0]
        if exe not in allowed:
            return {"ok": False, "error": f"command not allowed: {exe}"}
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
    roots = [r.strip() for r in (S.TOOLS_FS_ROOTS or "").split(",") if r.strip()]
    if not roots:
        return {"ok": False, "error": "fs tool not configured (TOOLS_FS_ROOTS empty)"}

    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(roots[0]) / p
        p = p.resolve()

        allowed_root = False
        for r in roots:
            try:
                root_path = Path(r).resolve()
                p.relative_to(root_path)
                allowed_root = True
                break
            except Exception:
                continue
        if not allowed_root:
            return {"ok": False, "error": "path outside allowed roots"}

        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        with open(p, "rb") as f:
            data = f.read(max_bytes + 1)

        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return {"ok": True, "path": str(p), "truncated": truncated, "content": text}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    if not S.TOOLS_ALLOW_FS_WRITE:
        return {"ok": False, "error": "fs write disabled"}
    path = args.get("path")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    roots = [r.strip() for r in (S.TOOLS_FS_ROOTS or "").split(",") if r.strip()]
    if not roots:
        return {"ok": False, "error": "fs tool not configured (TOOLS_FS_ROOTS empty)"}

    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(roots[0]) / p
        p = p.resolve()

        allowed_root = False
        for r in roots:
            try:
                root_path = Path(r).resolve()
                p.relative_to(root_path)
                allowed_root = True
                break
            except Exception:
                continue
        if not allowed_root:
            return {"ok": False, "error": "path outside allowed roots"}

        # Basic size limit to avoid large writes.
        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        if len(content.encode("utf-8")) > max_bytes:
            return {"ok": False, "error": f"content too large (>{max_bytes} bytes)"}

        os.makedirs(str(p.parent), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": str(p)}
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


def tool_git(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_GIT:
        return {"ok": False, "error": "git tool disabled"}

    argv = args.get("args")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        return {"ok": False, "error": "args must be a non-empty list of strings"}

    subcmd = argv[0].strip()
    allowed_subcmds = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
    if subcmd not in allowed_subcmds:
        return {"ok": False, "error": f"git subcommand not allowed: {subcmd}"}

    cwd = (S.TOOLS_GIT_CWD or "").strip() or S.TOOLS_SHELL_CWD
    os.makedirs(cwd, exist_ok=True)

    try:
        cp = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=S.TOOLS_GIT_TIMEOUT_SEC,
            check=False,
        )
        return {
            "ok": True,
            "returncode": cp.returncode,
            "stdout": cp.stdout[-20000:],
            "stderr": cp.stderr[-20000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {S.TOOLS_GIT_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


TOOL_IMPL = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "http_fetch": tool_http_fetch,
    "git": tool_git,
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
    if S.TOOLS_ALLOW_GIT:
        allowed.add("git")
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
    "git": {
        "name": "git",
        "description": "Run a limited set of git subcommands in a configured repo directory.",
        "parameters": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
            "required": ["args"],
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


def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool with validation + replay ID + deterministic logging."""

    if name not in TOOL_IMPL:
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    if not is_tool_allowed(name):
        raise HTTPException(status_code=403, detail=f"tool not allowed: {name}")

    sch = TOOL_SCHEMAS.get(name)
    if sch and isinstance(sch.get("parameters"), dict):
        errs = _validate_against_schema(sch["parameters"], args)
        if errs:
            raise HTTPException(status_code=400, detail={"error": "invalid tool arguments", "issues": errs})

    replay_id = new_id("tool")
    ts = now_unix()
    t0 = time.monotonic()

    out: Dict[str, Any]
    try:
        out = TOOL_IMPL[name](args)
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    dur_ms = (time.monotonic() - t0) * 1000.0

    event = {
        "ts": ts,
        "replay_id": replay_id,
        "tool": name,
        "ok": bool(out.get("ok")) if isinstance(out, dict) else False,
        "duration_ms": round(dur_ms, 1),
        "args": _truncate(args, max_chars=10_000),
        "result": _truncate(out, max_chars=20_000),
    }
    _log_tool_event(event)

    # Backward-compatible response shape, with replay_id attached.
    if isinstance(out, dict):
        return {"replay_id": replay_id, **out}
    return {"replay_id": replay_id, "ok": False, "error": "invalid tool result"}


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


@router.post("/v1/tools")
async def v1_tools_dispatch(req: Request):
    """Dispatcher endpoint.

    Body:
      {"name": "read_file", "arguments": {...}}
    """

    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="name must be a non-empty string")
    args = body.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="arguments must be an object")
    return _execute_tool(name.strip(), args)


@router.post("/v1/tools/{name}")
async def v1_tools_exec(req: Request, name: str):
    require_bearer(req)
    body = await req.json()
    tr = ToolExecRequest(**body)
    return _execute_tool(name, tr.arguments)
