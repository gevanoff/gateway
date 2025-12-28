import os
import json
import shlex
import subprocess
import logging
import sys
from typing import Any, Dict, List, Optional, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

import sqlite3
import time
import math
import hashlib
from array import array

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/var/lib/gateway/app/.env", extra="ignore")

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    MLX_BASE_URL: str = "http://127.0.0.1:10240/v1"

    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8800
    GATEWAY_BEARER_TOKEN: str

    DEFAULT_BACKEND: Literal["ollama", "mlx"] = "ollama"
    OLLAMA_MODEL_DEFAULT: str = "qwen2.5:32b"
    MLX_MODEL_DEFAULT: str = "mlx-community/gemma-2-2b-it-8bit"

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_SHELL_CWD: str = "/var/lib/gateway/tools"
    TOOLS_SHELL_TIMEOUT_SEC: int = 20

    EMBEDDINGS_BACKEND: Literal["ollama", "mlx"] = "ollama"
    EMBEDDINGS_MODEL: str = "nomic-embed-text"

    MEMORY_ENABLED: bool = True
    MEMORY_DB_PATH: str = "/var/lib/gateway/data/memory.sqlite"
    MEMORY_TOP_K: int = 6
    MEMORY_MIN_SIM: float = 0.25
    MEMORY_MAX_CHARS: int = 6000



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


TOOL_IMPL = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
}


def run_tool_call(name: str, arguments_json: str) -> Dict[str, Any]:
    fn = TOOL_IMPL.get(name)
    if not fn:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception:
        return {"ok": False, "error": "tool arguments must be valid JSON"}
    return fn(args)


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
        "id": "ollama-chat",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": msg, "finish_reason": out.get("done_reason", "stop")}],
        "model": model_name,
    }

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
    data["data"].append({"id": "ollama", "object": "model"})
    data["data"].append({"id": "mlx", "object": "model"})

    return data

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

    res = await memory_search(last_user, S.MEMORY_TOP_K, S.MEMORY_MIN_SIM)
    if not res.get("ok") or not res.get("results"):
        return messages

    chunks = []
    total = 0
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


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    require_bearer(req)
    body = await req.json()
    cc = ChatCompletionRequest(**body)
    cc.messages = await inject_memory(cc.messages)


    backend = choose_backend(cc.model)
    model_name = normalize_model(cc.model, backend)

    # Force non-stream for now (keep it simple and deterministic)
    if cc.stream:
        raise HTTPException(status_code=400, detail="stream=true not supported yet")

    # 1) Backend override via header (highest priority)
    backend_hdr = (req.headers.get("x-backend") or "").strip().lower()
    if backend_hdr in {"ollama", "mlx"}:
        backend: Literal["ollama", "mlx"] = backend_hdr  # type: ignore[assignment]
    else:
        # 2) Backend by model prefix / alias
        backend = choose_backend(cc.model)

        # 3) Optional auto-policy (only when no explicit prefix/alias forced it)
        # If model explicitly indicates a backend, do not override it.
        model_l = (cc.model or "").strip().lower()
        explicitly_pinned = model_l.startswith(("ollama:", "mlx:")) or model_l in {"ollama", "mlx", "ollama-default", "mlx-default"}
        if not explicitly_pinned:
            if cc.tools:
                backend = "ollama"
            else:
                size = approx_text_size(cc.messages)
                # tune threshold; start conservative
                if size >= 40_000:
                    backend = "mlx"

    model_name = normalize_model(cc.model, backend)

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

    # If tools are present, run gateway tool-loop; otherwise just proxy.
    if cc.tools:
        resp = await tool_loop(cc, backend, model_name)
    else:
        resp = await (call_mlx_openai(cc) if backend == "mlx" else call_ollama(cc, model_name))

    # Optional: surface routing decision to the client
    out = JSONResponse(resp)
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = model_name
    return out

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

