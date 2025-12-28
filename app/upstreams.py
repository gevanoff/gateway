from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Literal

import httpx
from fastapi import HTTPException

from app.config import S, logger
from app.models import ChatCompletionRequest
from app.openai_utils import new_id, now_unix, sse, sse_done


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
        "id": new_id("chatcmpl"),
        "object": "chat.completion",
        "created": now_unix(),
        "choices": [{"index": 0, "message": msg, "finish_reason": out.get("done_reason", "stop")}],
        "model": model_name,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def embed_ollama(texts: List[str], model: str) -> List[List[float]]:
    async with httpx.AsyncClient(timeout=600) as client:
        try:
            r = await client.post(
                f"{S.OLLAMA_BASE_URL}/api/embed",
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            j = r.json()
            embs = j.get("embeddings")
            if isinstance(embs, list) and embs and isinstance(embs[0], list):
                return embs
        except Exception:
            pass

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
                raise HTTPException(status_code=502, detail={"upstream": "ollama", "error": "No embedding in response"})
            out.append(e)
        return out


async def embed_mlx(texts: List[str], model: str) -> List[List[float]]:
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(
            f"{S.MLX_BASE_URL}/embeddings",
            json={"model": model, "input": texts if len(texts) > 1 else texts[0]},
        )
        r.raise_for_status()
        j = r.json()
        data = j.get("data", [])
        out: List[List[float]] = []
        for item in data:
            emb = (item or {}).get("embedding")
            if isinstance(emb, list):
                out.append(emb)
        if len(out) != len(texts):
            raise HTTPException(status_code=502, detail={"upstream": "mlx", "error": "Unexpected embeddings shape"})
        return out


async def embed_text_for_memory(text: str) -> list[float]:
    model = S.EMBEDDINGS_MODEL
    if S.EMBEDDINGS_BACKEND == "ollama":
        return (await embed_ollama([text], model))[0]
    return (await embed_mlx([text], model))[0]


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
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": "mlx", "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()


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

    stream_id = new_id("chatcmpl")
    created = now_unix()
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
                    yield sse(chunk)

                    if j.get("done") is True:
                        finish = j.get("done_reason") or "stop"
                        final_chunk = {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        }
                        yield sse(final_chunk)
                        break
        except httpx.HTTPStatusError as e:
            detail = {"upstream": "ollama", "status": e.response.status_code, "body": e.response.text[:5000]}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
        except httpx.RequestError as e:
            detail = {"upstream": "ollama", "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
        finally:
            yield sse_done()
