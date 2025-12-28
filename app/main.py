import os
import json
import shlex
import subprocess
import logging
import sys
import secrets
from typing import Any, Dict, List, Optional, Literal, AsyncIterator
from urllib.parse import urlparse
import base64

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

import sqlite3
import time
import math
import hashlib
from array import array

from app.router import RouterConfig, decide_route
from app import memory_v2

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/var/lib/gateway/app/.env", extra="ignore")

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    MLX_BASE_URL: str = "http://127.0.0.1:10240/v1"

    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8800
    GATEWAY_BEARER_TOKEN: str

    DEFAULT_BACKEND: Literal["ollama", "mlx"] = "ollama"
    # Backends can each have "strong" and "fast" model choices.
    # Defaults bias toward correctness for tools and general chat.
    OLLAMA_MODEL_STRONG: str = "qwen2.5:32b"
    OLLAMA_MODEL_FAST: str = "qwen2.5:7b"
    MLX_MODEL_STRONG: str = "mlx-community/gemma-2-2b-it-8bit"
    MLX_MODEL_FAST: str = "mlx-community/gemma-2-2b-it-8bit"

    # Legacy aliases kept for backward compatibility
    OLLAMA_MODEL_DEFAULT: str = "qwen2.5:32b"
    MLX_MODEL_DEFAULT: str = "mlx-community/gemma-2-2b-it-8bit"

    ROUTER_LONG_CONTEXT_CHARS: int = 40_000

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_ALLOW_HTTP_FETCH: bool = False
    # Optional explicit allowlist; if set, only these tools may be executed.
    # Example: "read_file,write_file,http_fetch"
    TOOLS_ALLOWLIST: str = ""
    TOOLS_SHELL_CWD: str = "/var/lib/gateway/tools"
    TOOLS_SHELL_TIMEOUT_SEC: int = 20

    TOOLS_HTTP_ALLOWED_HOSTS: str = "127.0.0.1,localhost"
    TOOLS_HTTP_TIMEOUT_SEC: int = 10
    TOOLS_HTTP_MAX_BYTES: int = 200_000

    EMBEDDINGS_BACKEND: Literal["ollama", "mlx"] = "ollama"
    EMBEDDINGS_MODEL: str = "nomic-embed-text"

    MEMORY_ENABLED: bool = True
    MEMORY_DB_PATH: str = "/var/lib/gateway/data/memory.sqlite"
    MEMORY_TOP_K: int = 6
    MEMORY_MIN_SIM: float = 0.25
    MEMORY_MAX_CHARS: int = 6000

    MEMORY_V2_ENABLED: bool = True
    MEMORY_V2_MAX_AGE_SEC: int = 60 * 60 * 24 * 30  # 30 days
    MEMORY_V2_TYPES_DEFAULT: str = "fact,preference,project"



S = Settings()
app = FastAPI(title="Local AI Gateway", version="0.1")

# ---------------------------
# Logging
# ---------------------------

logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper())


@app.middleware("http")
async def log_requests(req: Request, call_next):
    start = time.time()
    resp = None
    try:
        resp = await call_next(req)
        return resp
    finally:
        dur_ms = (time.time() - start) * 1000.0
        status = resp.status_code if resp is not None else 500
        path = req.url.path
        method = req.method
        logger.info("%s %s -> %d (%.1fms)", method, path, status, dur_ms)

# ---------------------------
# Memory (SQLite + vectors)
# ---------------------------

def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(S.MEMORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(S.MEMORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def memory_init() -> None:
    conn = _db()
    conn.execute("""
      CREATE TABLE IF NOT EXISTS memory (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        meta TEXT,
        emb BLOB NOT NULL,
        dim INTEGER NOT NULL,
        ts INTEGER NOT NULL
      )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(ts);")
    conn.commit()
    conn.close()

def pack_emb(vec: list[float]) -> bytes:
    a = array("f", [float(x) for x in vec])
    return a.tobytes()

def unpack_emb(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)

def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = na = nb = 0.0
    for i in range(len(a)):
        x = a[i]; y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / (math.sqrt(na) * math.sqrt(nb))

# ---------------------------
# Memory Init before usage
# ---------------------------

memory_init()

if S.MEMORY_V2_ENABLED:
    memory_v2.init(S.MEMORY_DB_PATH)

# ---------------------------
# Auth
# ---------------------------
def require_bearer(req: Request) -> None:
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token != S.GATEWAY_BEARER_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


# ---------------------------
# OpenAI-ish request models
# ---------------------------
class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[Any] = None
    tool_call_id: Optional[str] = None


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Dict[str, Any]


class ToolSpec(BaseModel):
    type: str = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    tools: Optional[List[ToolSpec]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

class EmbeddingsRequest(BaseModel):
    model: str
    input: Any  # str | list[str]


class MemoryUpsertRequest(BaseModel):
    type: Literal["fact", "preference", "project", "ephemeral"]
    text: str
    source: Optional[Literal["user", "system", "tool"]] = "user"
    meta: Optional[Dict[str, Any]] = None
    id: Optional[str] = None
    ts: Optional[int] = None


class MemorySearchRequest(BaseModel):
    query: str
    types: Optional[List[Literal["fact", "preference", "project", "ephemeral"]]] = None
    sources: Optional[List[Literal["user", "system", "tool"]]] = None
    top_k: Optional[int] = None
    min_sim: Optional[float] = None
    max_age_sec: Optional[int] = None
    include_compacted: bool = False


class MemoryCompactRequest(BaseModel):
    types: Optional[List[Literal["fact", "preference", "project", "ephemeral"]]] = None
    max_age_sec: Optional[int] = None
    max_items: int = 50
    target_type: Literal["fact", "preference", "project", "ephemeral"] = "project"
    target_source: Literal["user", "system", "tool"] = "system"
    include_compacted: bool = False

class CompletionRequest(BaseModel):
    model: str
    prompt: Any  # str | list[str]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: Optional[bool] = False

class RerankRequest(BaseModel):
    model: Optional[str] = None
    query: str
    documents: List[str]
    top_n: Optional[int] = None


def _now_unix() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _sse(data_obj: Any) -> bytes:
    return f"data: {json.dumps(data_obj, separators=(',', ':'))}\n\n".encode("utf-8")


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# ---------------------------
# Tool executors (gateway-owned)
# ---------------------------
def tool_shell(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_SHELL:
        return {"ok": False, "error": "shell tool disabled"}

    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "error": "cmd must be a non-empty string"}

    cwd = S.TOOLS_SHELL_CWD
    os.makedirs(cwd, exist_ok=True)

    try:
        # Use shlex to split safely; no shell=True
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


def _is_tool_allowed(name: str) -> bool:
    return name in _allowed_tool_names()


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
    if not _is_tool_allowed(name):
        return {"ok": False, "error": f"tool not allowed: {name}"}
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception:
        return {"ok": False, "error": "tool arguments must be valid JSON"}
    return fn(args)


class ToolExecRequest(BaseModel):
    arguments: Dict[str, Any] = {}


@app.get("/v1/tools")
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


@app.post("/v1/tools/{name}")
async def v1_tools_exec(req: Request, name: str):
    require_bearer(req)
    if name not in TOOL_IMPL:
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    if not _is_tool_allowed(name):
        raise HTTPException(status_code=403, detail=f"tool not allowed: {name}")
    body = await req.json()
    tr = ToolExecRequest(**body)
    # Execute synchronously; tool implementations enforce their own safety toggles.
    return TOOL_IMPL[name](tr.arguments)


# ---------------------------
# Routing
# ---------------------------

def choose_backend(model: str) -> Literal["ollama", "mlx"]:
    m = (model or "").strip().lower()

    # Explicit prefixes win
    if m.startswith("ollama:"):
        return "ollama"
    if m.startswith("mlx:"):
        return "mlx"

    # Alias forms
    if m in {"ollama", "ollama-default"}:
        return "ollama"
    if m in {"mlx", "mlx-default"}:
        return "mlx"

    return S.DEFAULT_BACKEND


def choose_backend_from_request(req: Request, model: str) -> Literal["ollama", "mlx"]:
    h = (req.headers.get("x-backend") or "").strip().lower()
    if h in {"ollama", "mlx"}:
        return h  # type: ignore[return-value]
    return choose_backend(model)


def normalize_model(model: str, backend: str) -> str:
    m = (model or "").strip()

    if backend == "ollama":
        if m.startswith("ollama:"):
            m = m[len("ollama:") :]
        if m in {"default", "ollama", ""}:
            return S.OLLAMA_MODEL_DEFAULT
        return m

    # mlx
    if m.startswith("mlx:"):
        m = m[len("mlx:") :]
    if m in {"default", "mlx", ""}:
        return S.MLX_MODEL_DEFAULT
    return m


def _router_cfg() -> RouterConfig:
    return RouterConfig(
        default_backend=S.DEFAULT_BACKEND,
        ollama_strong_model=S.OLLAMA_MODEL_STRONG,
        ollama_fast_model=S.OLLAMA_MODEL_FAST,
        mlx_strong_model=S.MLX_MODEL_STRONG,
        mlx_fast_model=S.MLX_MODEL_FAST,
        long_context_chars_threshold=S.ROUTER_LONG_CONTEXT_CHARS,
    )

def approx_text_size(messages: List[ChatMessage]) -> int:
    # crude but effective; counts characters of user+system+assistant content
    n = 0
    for m in messages:
        c = m.content
        if isinstance(c, str):
            n += len(c)
        elif c is None:
            continue
        else:
            n += len(json.dumps(c))
    return n


# ---------------------------
# Backend callers
# ---------------------------

async def call_mlx_openai(req: ChatCompletionRequest) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=600) as client:
        try:
            r = await client.post(
                f"{S.MLX_BASE_URL}/chat/completions",
                json=req.model_dump(exclude_none=True),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "mlx", "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": "mlx", "error": str(e)})


async def call_ollama(req: ChatCompletionRequest, model_name: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
        "stream": False,
    }
    if req.tools:
        payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
    if req.temperature is not None:
        payload.setdefault("options", {})["temperature"] = req.temperature

    async with httpx.AsyncClient(timeout=600) as client:
        try:
            r = await client.post(f"{S.OLLAMA_BASE_URL}/api/chat", json=payload)
            r.raise_for_status()
            out = r.json()
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "ollama", "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": "ollama", "error": str(e)})

    msg = out.get("message", {})
    return {
        "id": _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": _now_unix(),
        "choices": [{"index": 0, "message": msg, "finish_reason": out.get("done_reason", "stop")}],
        "model": model_name,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_mlx_openai_chat(payload: Dict[str, Any]) -> AsyncIterator[bytes]:
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream(
                "POST",
                f"{S.MLX_BASE_URL}/chat/completions",
                json=payload,
                headers={"accept": "text/event-stream"},
            ) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "mlx", "status": e.response.status_code, "body": e.response.text[:5000]}
            yield _sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield _sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": "mlx", "error": str(e)}
            yield _sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield _sse_done()


async def stream_ollama_chat_as_openai(req: ChatCompletionRequest, model_name: str) -> AsyncIterator[bytes]:
    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
        "stream": True,
    }
    if req.tools:
        payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
    if req.temperature is not None:
        payload.setdefault("options", {})["temperature"] = req.temperature

    stream_id = _new_id("chatcmpl")
    created = _now_unix()
    sent_role = False

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", f"{S.OLLAMA_BASE_URL}/api/chat", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue

                    msg = (j or {}).get("message") or {}
                    delta_content = msg.get("content")
                    if not isinstance(delta_content, str):
                        delta_content = ""

                    delta: Dict[str, Any] = {"content": delta_content}
                    if not sent_role:
                        delta["role"] = "assistant"
                        sent_role = True

                    chunk = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield _sse(chunk)

                    if j.get("done") is True:
                        finish = j.get("done_reason") or "stop"
                        final_chunk = {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        }
                        yield _sse(final_chunk)
                        break
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "ollama", "status": e.response.status_code, "body": e.response.text[:5000]}
            yield _sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
        except httpx.RequestError as e:
            detail = {"upstream": "ollama", "error": str(e)}
            yield _sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
        finally:
            yield _sse_done()

async def embed_ollama(texts: List[str], model: str) -> List[List[float]]:
    # Try modern Ollama endpoints; fall back if needed.
    async with httpx.AsyncClient(timeout=600) as client:
        # 1) /api/embed (newer)
        try:
            r = await client.post(
                f"{S.OLLAMA_BASE_URL}/api/embed",
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            j = r.json()
            # expected: {"embeddings":[[...],[...]]}
            embs = j.get("embeddings")
            if isinstance(embs, list) and embs and isinstance(embs[0], list):
                return embs
        except Exception:
            pass

        # 2) /api/embeddings (older; usually single prompt)
        out: List[List[float]] = []
        for t in texts:
            r = await client.post(
                f"{S.OLLAMA_BASE_URL}/api/embeddings",
                json={"model": model, "prompt": t},
            )
            r.raise_for_status()
            j = r.json()
            e = j.get("embedding")
            if not isinstance(e, list):
                raise HTTPException(status_code=502, detail={"upstream":"ollama","error":"No embedding in response"})
            out.append(e)
        return out


async def embed_mlx(texts: List[str], model: str) -> List[List[float]]:
    # OpenAI-compatible embeddings endpoint (if MLX server supports it)
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(
            f"{S.MLX_BASE_URL}/embeddings",
            json={"model": model, "input": texts if len(texts) > 1 else texts[0]},
        )
        r.raise_for_status()
        j = r.json()
        data = j.get("data", [])
        # expected: data=[{"embedding":[...], ...}, ...]
        out: List[List[float]] = []
        for item in data:
            emb = (item or {}).get("embedding")
            if isinstance(emb, list):
                out.append(emb)
        if len(out) != len(texts):
            raise HTTPException(status_code=502, detail={"upstream":"mlx","error":"Unexpected embeddings shape"})
        return out


async def embed_text_for_memory(text: str) -> list[float]:
    model = S.EMBEDDINGS_MODEL
    if S.EMBEDDINGS_BACKEND == "ollama":
        return (await embed_ollama([text], model))[0]
    return (await embed_mlx([text], model))[0]


async def embed_text_for_memory_v2(text: str) -> list[float]:
    return await embed_text_for_memory(text)


def _memory_v2_default_types() -> list[memory_v2.MemoryType]:
    raw = (S.MEMORY_V2_TYPES_DEFAULT or "").strip()
    if not raw:
        return ["fact", "preference", "project"]
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    out: list[memory_v2.MemoryType] = []
    for p in parts:
        if p in {"fact", "preference", "project", "ephemeral"}:
            out.append(p)  # type: ignore[arg-type]
    return out or ["fact", "preference", "project"]

async def memory_upsert(text: str, meta: dict | None = None, mid: str | None = None) -> dict:
    meta = meta or {}
    if mid is None:
        mid = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    emb = await embed_text_for_memory(text)
    blob = pack_emb(emb)

    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO memory(id,text,meta,emb,dim,ts) VALUES(?,?,?,?,?,?)",
        (mid, text, json.dumps(meta), blob, len(emb), int(time.time())),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": mid, "dim": len(emb)}

async def memory_search(query: str, k: int, min_sim: float) -> dict:
    qemb = await embed_text_for_memory(query)

    conn = _db()
    rows = conn.execute("SELECT id,text,meta,emb,dim,ts FROM memory").fetchall()
    conn.close()

    scored = []
    for (mid, text, meta, emb_blob, dim, ts) in rows:
        if dim != len(qemb):
            continue
        emb = unpack_emb(emb_blob)
        s = cosine(qemb, emb)
        if s >= min_sim:
            scored.append((s, mid, text, meta, ts))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for (s, mid, text, meta, ts) in scored[:k]:
        out.append({
            "score": s,
            "id": mid,
            "text": text,
            "meta": json.loads(meta) if meta else None,
            "ts": ts
        })
    return {"ok": True, "results": out}


# ---------------------------
# Tool-loop (gateway-level agent behavior)
# ---------------------------
async def tool_loop(initial_req: ChatCompletionRequest, backend: str, model_name: str, max_steps: int = 8) -> Dict[str, Any]:
    req = initial_req
    for _ in range(max_steps):
        if backend == "mlx":
            resp = await call_mlx_openai(req)
        else:
            resp = await call_ollama(req, model_name)

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {}) or {}
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            return resp

        # Execute tool calls and append tool messages
        new_messages = list(req.messages)
        new_messages.append(ChatMessage(**msg))  # assistant message with tool_calls

        for tc in tool_calls:
            # OpenAI tool call format: {id, type:"function", function:{name, arguments}}
            fn = (tc or {}).get("function") or {}
            name = fn.get("name")
            arguments = fn.get("arguments", "")
            result = run_tool_call(name, arguments)
            new_messages.append(ChatMessage(role="tool", tool_call_id=tc.get("id"), content=json.dumps(result)))

        req = ChatCompletionRequest(
            model=req.model,
            messages=new_messages,
            tools=req.tools,
            tool_choice=req.tool_choice,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            stream=False,
        )

    raise HTTPException(status_code=500, detail="tool loop exceeded max_steps")


@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/health/upstreams")
async def health_upstreams(req: Request):
    require_bearer(req)

    results: Dict[str, Any] = {"ok": True, "upstreams": {}}

    async with httpx.AsyncClient(timeout=10) as client:
        # Ollama: /api/tags
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            results["upstreams"]["ollama"] = {
                "ok": True,
                "status": r.status_code,
            }
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["ollama"] = {"ok": False, "error": str(e)}

        # MLX OpenAI server: /v1/models
        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            results["upstreams"]["mlx"] = {
                "ok": True,
                "status": r.status_code,
            }
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["mlx"] = {"ok": False, "error": str(e)}

    return results


@app.get("/v1/models")
async def list_models(req: Request):
    require_bearer(req)

    now = _now_unix()
    data = {"object": "list", "data": []}

    async with httpx.AsyncClient(timeout=30) as client:
        # Ollama models
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = r.json().get("models", [])
            for m in models:
                name = m.get("name")
                if name:
                    data["data"].append({"id": f"ollama:{name}", "object": "model"})
        except Exception:
            pass

        # MLX models
        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            models = r.json().get("data", [])
            for m in models:
                mid = m.get("id")
                if mid:
                    data["data"].append({"id": f"mlx:{mid}", "object": "model"})
        except Exception:
            pass

    # Optional aliases
    data["data"].append({"id": "ollama", "object": "model", "created": now, "owned_by": "gateway"})
    data["data"].append({"id": "mlx", "object": "model", "created": now, "owned_by": "gateway"})

    for m in data["data"]:
        m.setdefault("created", now)
        m.setdefault("owned_by", "local")

    return data


@app.get("/v1/models/{model_id}")
async def get_model(req: Request, model_id: str):
    require_bearer(req)
    now = _now_unix()
    return {"id": model_id, "object": "model", "created": now, "owned_by": "local"}

async def inject_memory(messages: List[ChatMessage]) -> List[ChatMessage]:
    if not S.MEMORY_ENABLED:
        return messages

    last_user = None
    for m in reversed(messages):
        if m.role == "user":
            last_user = m.content
            break
    if not isinstance(last_user, str) or not last_user.strip():
        return messages

    chunks = []
    total = 0

    if S.MEMORY_V2_ENABLED:
        # v2 async retrieval
        qemb = await embed_text_for_memory_v2(last_user)
        now = int(time.time())
        types = _memory_v2_default_types()
        max_age = S.MEMORY_V2_MAX_AGE_SEC
        # manually search using v2 module but with already-computed query embedding
        # (avoid embedding twice)
        conn = sqlite3.connect(S.MEMORY_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        clause = " WHERE compacted_into IS NULL AND type IN (%s) AND ts >= ?" % ",".join(["?"] * len(types))
        args: list[Any] = [*types, int(now - int(max_age))]
        rows = conn.execute(f"SELECT id,type,source,text,meta,emb,dim,ts FROM memory_v2{clause}", args).fetchall()
        conn.close()

        scored = []
        for (mid, mtype, source, text, meta, emb_blob, dim, ts) in rows:
            if dim != len(qemb):
                continue
            emb = memory_v2.unpack_emb(emb_blob)
            s = memory_v2.cosine(qemb, emb)
            if s >= S.MEMORY_MIN_SIM:
                scored.append((s, mid, mtype, source, text, ts))
        scored.sort(key=lambda x: x[0], reverse=True)

        for (s, mid, mtype, source, text, ts) in scored[: S.MEMORY_TOP_K]:
            if not isinstance(text, str):
                continue
            line = f"- ({mtype}/{source}, {s:.3f}) {text}"
            if total + len(line) > S.MEMORY_MAX_CHARS:
                break
            chunks.append(line)
            total += len(line)
    else:
        res = await memory_search(last_user, S.MEMORY_TOP_K, S.MEMORY_MIN_SIM)
        if not res.get("ok") or not res.get("results"):
            return messages
        for r in res["results"]:
            t = r.get("text") or ""
            if not isinstance(t, str):
                continue
            line = f"- ({r.get('score'):.3f}) {t}"
            if total + len(line) > S.MEMORY_MAX_CHARS:
                break
            chunks.append(line)
            total += len(line)

    if not chunks:
        return messages

    mem_text = "Retrieved memory (may be relevant):\n" + "\n".join(chunks)
    return [ChatMessage(role="system", content=mem_text)] + messages


@app.post("/v1/memory/upsert")
async def v1_memory_upsert(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    mr = MemoryUpsertRequest(**body)
    if not isinstance(mr.text, str) or not mr.text.strip():
        raise HTTPException(status_code=400, detail="text must be non-empty")

    emb = await embed_text_for_memory_v2(mr.text)
    out = memory_v2.upsert(
        db_path=S.MEMORY_DB_PATH,
        embed=lambda _t: emb,
        text=mr.text,
        mtype=mr.type,
        source=(mr.source or "user"),
        meta=mr.meta,
        mid=mr.id,
        ts=mr.ts,
    )
    return out


@app.get("/v1/memory/list")
async def v1_memory_list(
    req: Request,
    type: Optional[str] = None,
    source: Optional[str] = None,
    since_ts: Optional[int] = None,
    max_age_sec: Optional[int] = None,
    limit: int = 50,
    include_compacted: bool = False,
):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    types = None
    if type:
        parts = [p.strip().lower() for p in type.split(",") if p.strip()]
        types = [p for p in parts if p in {"fact", "preference", "project", "ephemeral"}]  # type: ignore[assignment]

    sources = None
    if source:
        parts = [p.strip().lower() for p in source.split(",") if p.strip()]
        sources = [p for p in parts if p in {"user", "system", "tool"}]  # type: ignore[assignment]

    return memory_v2.list_items(
        db_path=S.MEMORY_DB_PATH,
        types=types,
        sources=sources,
        since_ts=since_ts,
        max_age_sec=max_age_sec,
        limit=max(1, min(int(limit), 500)),
        include_compacted=bool(include_compacted),
    )


@app.post("/v1/memory/search")
async def v1_memory_search(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    sr = MemorySearchRequest(**body)
    if not sr.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")

    qemb = await embed_text_for_memory_v2(sr.query)

    # Avoid embedding twice: pass a lambda that returns precomputed embedding for the query,
    # but memory_v2.search embeds the query internally. Implement the search inline.
    types = sr.types
    sources = sr.sources
    top_k = int(sr.top_k or S.MEMORY_TOP_K)
    min_sim = float(sr.min_sim if sr.min_sim is not None else S.MEMORY_MIN_SIM)
    max_age = int(sr.max_age_sec if sr.max_age_sec is not None else S.MEMORY_V2_MAX_AGE_SEC)

    now = int(time.time())
    where = []
    args: list[Any] = []
    if not sr.include_compacted:
        where.append("compacted_into IS NULL")
    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))
    if sources:
        where.append("source IN (%s)" % ",".join(["?"] * len(sources)))
        args.extend(list(sources))
    if max_age > 0:
        where.append("ts >= ?")
        args.append(int(now - max_age))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(S.MEMORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    rows = conn.execute(f"SELECT id,type,source,text,emb,dim,ts FROM memory_v2{clause}", args).fetchall()
    conn.close()

    scored = []
    for (mid, mtype, source, text, emb_blob, dim, ts) in rows:
        if dim != len(qemb):
            continue
        emb = memory_v2.unpack_emb(emb_blob)
        s = memory_v2.cosine(qemb, emb)
        if s >= min_sim:
            scored.append((s, mid, mtype, source, text, ts))
    scored.sort(key=lambda x: x[0], reverse=True)

    out = []
    for (s, mid, mtype, source, text, ts) in scored[: max(1, min(top_k, 100))]:
        out.append({"score": float(s), "id": mid, "type": mtype, "source": source, "text": text, "ts": ts})
    return {"ok": True, "results": out}


async def _summarize_for_compaction(items: list[dict], backend: Literal["ollama", "mlx"], model_name: str) -> str:
    # Keep prompt deterministic and “future-proof”: summary is structured and not overly lossy.
    lines = []
    for it in items:
        t = it.get("type")
        s = it.get("source")
        ts = it.get("ts")
        text = it.get("text")
        if not isinstance(text, str):
            continue
        lines.append(f"[{t}/{s} @ {ts}] {text}")

    sys_prompt = (
        "You are compacting an agent memory store. Produce a concise set of durable entries. "
        "Rules: (1) preserve factual correctness, (2) keep preferences explicit, (3) keep project context actionable, "
        "(4) avoid personal data, (5) do not invent. Output plain text, up to 25 bullet points."
    )
    user_text = "Memories to compact:\n" + "\n".join(lines)

    cc = ChatCompletionRequest(
        model=model_name,
        messages=[
            ChatMessage(role="system", content=sys_prompt),
            ChatMessage(role="user", content=user_text),
        ],
        stream=False,
    )

    resp = await (call_mlx_openai(cc) if backend == "mlx" else call_ollama(cc, model_name))
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content")
    return content if isinstance(content, str) else ""


@app.post("/v1/memory/compact")
async def v1_memory_compact(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    cr = MemoryCompactRequest(**body)

    # Select candidates
    now = int(time.time())
    max_age = int(cr.max_age_sec if cr.max_age_sec is not None else S.MEMORY_V2_MAX_AGE_SEC)
    types = cr.types or _memory_v2_default_types()
    max_items = max(1, min(int(cr.max_items), 200))

    where = []
    args: list[Any] = []
    if not cr.include_compacted:
        where.append("compacted_into IS NULL")
    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))
    if max_age > 0:
        where.append("ts < ?")
        args.append(int(now - max_age))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(S.MEMORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    rows = conn.execute(
        f"SELECT id,type,source,text,meta,ts FROM memory_v2{clause} ORDER BY ts ASC LIMIT ?",
        (*args, max_items),
    ).fetchall()
    conn.close()

    items = []
    ids = []
    for (mid, mtype, source, text, meta, ts) in rows:
        ids.append(mid)
        items.append({"id": mid, "type": mtype, "source": source, "text": text, "meta": meta, "ts": ts})

    if len(items) < 2:
        return {"ok": True, "compacted": 0, "message": "not enough items to compact"}

    # Route summarization to a strong model.
    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=_router_cfg(),
        request_model="default",
        headers=hdrs,
        messages=[{"role": "user", "content": "\n".join([it["text"] for it in items if isinstance(it.get("text"), str)])}],
        has_tools=True,
    )
    backend: Literal["ollama", "mlx"] = route.backend
    model_name = route.model

    summary = await _summarize_for_compaction(items, backend, model_name)
    if not summary.strip():
        raise HTTPException(status_code=502, detail="compaction summarizer returned empty output")

    # Insert compacted entry
    emb = await embed_text_for_memory_v2(summary)
    new_meta = {"compacted_ids": ids, "router_reason": route.reason}
    out = memory_v2.upsert(
        db_path=S.MEMORY_DB_PATH,
        embed=lambda _t: emb,
        text=summary,
        mtype=cr.target_type,
        source=cr.target_source,
        meta=new_meta,
        mid=None,
        ts=int(time.time()),
    )
    new_id = out.get("id")
    if isinstance(new_id, str):
        memory_v2.mark_compacted(db_path=S.MEMORY_DB_PATH, ids=ids, into_id=new_id)
    return {"ok": True, "compacted": len(ids), "new_id": new_id}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    require_bearer(req)
    body = await req.json()
    cc = ChatCompletionRequest(**body)
    cc.messages = await inject_memory(cc.messages)

    # Policy router chooses backend/model unless request explicitly pins it.
    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=_router_cfg(),
        request_model=cc.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=bool(cc.tools),
    )
    backend: Literal["ollama", "mlx"] = route.backend
    model_name = route.model

    if cc.stream and cc.tools:
        raise HTTPException(status_code=400, detail="stream=true not supported when tools are provided")

    # Keep legacy normalization behavior for explicit model override values
    # (decide_route already normalizes defaults and prefixes).

    # For MLX OpenAI server, ensure the request model matches what it expects
    cc_routed = ChatCompletionRequest(
        model=model_name if backend == "mlx" else cc.model,  # MLX expects actual model id
        messages=cc.messages,
        tools=cc.tools,
        tool_choice=cc.tool_choice,
        temperature=cc.temperature,
        max_tokens=cc.max_tokens,
        stream=False,
    )

    # Streaming mode
    if cc.stream:
        if backend == "mlx":
            payload = cc_routed.model_dump(exclude_none=True)
            payload["stream"] = True
            gen = stream_mlx_openai_chat(payload)
        else:
            gen = stream_ollama_chat_as_openai(cc, model_name)

        out = StreamingResponse(gen, media_type="text/event-stream")
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        return out

    # Non-stream
    if cc.tools:
        resp = await tool_loop(cc, backend, model_name)
    else:
        resp = await (call_mlx_openai(cc_routed) if backend == "mlx" else call_ollama(cc, model_name))

    out = JSONResponse(resp)
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = model_name
    out.headers["X-Router-Reason"] = route.reason
    return out


@app.post("/v1/completions")
async def completions(req: Request):
    require_bearer(req)
    body = await req.json()
    cr = CompletionRequest(**body)

    if isinstance(cr.prompt, str):
        prompt_text = cr.prompt
    elif isinstance(cr.prompt, list) and all(isinstance(x, str) for x in cr.prompt):
        prompt_text = "\n".join(cr.prompt)
    else:
        raise HTTPException(status_code=400, detail="prompt must be a string or list of strings")

    cc = ChatCompletionRequest(
        model=cr.model,
        messages=[ChatMessage(role="user", content=prompt_text)],
        temperature=cr.temperature,
        max_tokens=cr.max_tokens,
        stream=bool(cr.stream),
    )

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=_router_cfg(),
        request_model=cc.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
    )
    backend: Literal["ollama", "mlx"] = route.backend
    model_name = route.model

    if cc.stream:
        stream_id = _new_id("cmpl")
        created = _now_unix()

        async def gen() -> AsyncIterator[bytes]:
            if backend == "mlx":
                payload = cc.model_dump(exclude_none=True)
                payload["model"] = model_name
                payload["stream"] = True
                async for chunk in stream_mlx_openai_chat(payload):
                    # MLX stream is chat SSE; map best-effort to text completions
                    # If it's already SSE, we parse minimal "data:" JSON lines.
                    for line in chunk.splitlines():
                        if not line.startswith(b"data:"):
                            continue
                        data = line[len(b"data:") :].strip()
                        if data == b"[DONE]":
                            yield _sse_done()
                            return
                        try:
                            j = json.loads(data)
                        except Exception:
                            continue
                        delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            yield _sse({
                                "id": stream_id,
                                "object": "text_completion",
                                "created": created,
                                "model": model_name,
                                "choices": [{"index": 0, "text": text, "finish_reason": None}],
                            })
                    # ignore non-data bytes
            else:
                async for sse_bytes in stream_ollama_chat_as_openai(cc, model_name):
                    for line in sse_bytes.splitlines():
                        if not line.startswith(b"data:"):
                            continue
                        data = line[len(b"data:") :].strip()
                        if data == b"[DONE]":
                            yield _sse_done()
                            return
                        try:
                            j = json.loads(data)
                        except Exception:
                            continue
                        delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            yield _sse({
                                "id": stream_id,
                                "object": "text_completion",
                                "created": created,
                                "model": model_name,
                                "choices": [{"index": 0, "text": text, "finish_reason": None}],
                            })
            yield _sse({
                "id": stream_id,
                "object": "text_completion",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
            })
            yield _sse_done()

        out = StreamingResponse(gen(), media_type="text/event-stream")
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        return out

    # Non-stream completion
    if backend == "mlx":
        cc_routed = ChatCompletionRequest(
            model=model_name,
            messages=cc.messages,
            temperature=cc.temperature,
            max_tokens=cc.max_tokens,
            stream=False,
        )
        chat_resp = await call_mlx_openai(cc_routed)
    else:
        chat_resp = await call_ollama(cc, model_name)

    msg = ((chat_resp.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content")
    if not isinstance(text, str):
        text = ""
    resp = {
        "id": _new_id("cmpl"),
        "object": "text_completion",
        "created": _now_unix(),
        "model": model_name,
        "choices": [{"index": 0, "text": text, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    out = JSONResponse(resp)
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = model_name
    out.headers["X-Router-Reason"] = route.reason
    return out


@app.post("/v1/rerank")
async def rerank(req: Request):
    require_bearer(req)
    body = await req.json()
    rr = RerankRequest(**body)

    if not rr.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")
    if not rr.documents:
        raise HTTPException(status_code=400, detail="documents must be non-empty")
    if any((not isinstance(d, str) or not d) for d in rr.documents):
        raise HTTPException(status_code=400, detail="documents must be a list of non-empty strings")

    top_n = rr.top_n if isinstance(rr.top_n, int) and rr.top_n > 0 else len(rr.documents)
    top_n = min(top_n, len(rr.documents))

    backend = S.EMBEDDINGS_BACKEND
    model_used = rr.model or S.EMBEDDINGS_MODEL

    try:
        if backend == "ollama":
            q_emb = (await embed_ollama([rr.query], model_used))[0]
            doc_embs = await embed_ollama(rr.documents, model_used)
        else:
            q_emb = (await embed_mlx([rr.query], model_used))[0]
            doc_embs = await embed_mlx(rr.documents, model_used)
    except httpx.HTTPStatusError as e:
        detail = {"upstream": backend, "status": e.response.status_code, "body": e.response.text[:5000]}
        logger.warning("/v1/rerank upstream HTTP error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)
    except httpx.RequestError as e:
        detail = {"upstream": backend, "error": str(e)}
        logger.warning("/v1/rerank upstream request error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)

    scored = []
    for i, emb in enumerate(doc_embs):
        s = cosine(q_emb, emb)
        scored.append((s, i))
    scored.sort(key=lambda x: x[0], reverse=True)

    data = []
    for rank, (score, i) in enumerate(scored[:top_n]):
        data.append({
            "index": i,
            "relevance_score": float(score),
            "document": rr.documents[i],
        })

    return {
        "object": "list",
        "data": data,
        "model": model_used,
    }

@app.post("/v1/embeddings")
async def embeddings(req: Request):
    require_bearer(req)
    body = await req.json()
    er = EmbeddingsRequest(**body)

    # Normalize input to list[str]
    if isinstance(er.input, str):
        texts = [er.input]
    elif isinstance(er.input, list) and all(isinstance(x, str) for x in er.input):
        texts = er.input
    else:
        raise HTTPException(status_code=400, detail="input must be a string or list of strings")

    backend = S.EMBEDDINGS_BACKEND
    model = er.model if er.model not in {"default", "", None} else S.EMBEDDINGS_MODEL

    try:
        if backend == "ollama":
            embs = await embed_ollama(texts, model)
        else:
            embs = await embed_mlx(texts, model)
    except httpx.HTTPStatusError as e:
        detail = {"upstream": backend, "status": e.response.status_code, "body": e.response.text[:5000]}
        logger.warning("/v1/embeddings upstream HTTP error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)
    except httpx.RequestError as e:
        detail = {"upstream": backend, "error": str(e)}
        logger.warning("/v1/embeddings upstream request error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)

    # OpenAI-ish response
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": embs[i]}
            for i in range(len(embs))
        ],
        "model": model,
    }

@app.post("/memory/upsert")
async def http_memory_upsert(req: Request):
    require_bearer(req)
    body = await req.json()
    text = body.get("text")
    meta = body.get("meta", {})
    mid = body.get("id")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text must be non-empty string")
    if mid is not None and not isinstance(mid, str):
        raise HTTPException(status_code=400, detail="id must be string")
    if meta is not None and not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="meta must be object")
    return await memory_upsert(text=text, meta=meta, mid=mid)

@app.post("/memory/search")
async def http_memory_search(req: Request):
    require_bearer(req)
    body = await req.json()
    query = body.get("query")
    k = int(body.get("k", S.MEMORY_TOP_K))
    min_sim = float(body.get("min_sim", S.MEMORY_MIN_SIM))
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty string")
    return await memory_search(query=query, k=k, min_sim=min_sim)

