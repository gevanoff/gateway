from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict

import httpx

from app.openai_utils import new_id, now_unix, sse, sse_done


async def passthrough_sse(resp: httpx.Response) -> AsyncIterator[bytes]:
    """
    Pass-through upstream SSE (already 'data: ...\n\n') from MLX-style OpenAI servers.
    """
    async for chunk in resp.aiter_bytes():
        if chunk:
            yield chunk


async def ollama_ndjson_to_openai_sse(
    resp: httpx.Response,
    *,
    model_name: str,
) -> AsyncIterator[bytes]:
    """
    Translate Ollama NDJSON streaming into OpenAI SSE chat.completion.chunk events.
    """
    chunk_id = new_id("chatcmpl")
    created = now_unix()
    sent_role = False

    async for line in resp.aiter_lines():
        if not line:
            continue

        obj = json.loads(line)

        # Ollama /api/chat uses "message": {"role":"assistant","content":"..."} and "done"
        # /api/generate uses "response": "..." and "done" :contentReference[oaicite:3]{index=3}
        done = bool(obj.get("done", False))

        # Prefer chat field
        content = None
        msg = obj.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")

        # Fallback to generate field
        if content is None:
            content = obj.get("response")

        if content:
            delta: Dict[str, Any] = {"content": content}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )

        if done:
            finish_reason = obj.get("done_reason") or "stop"
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                }
            )
            yield sse_done()
            return

    # If upstream ends without a done marker, still end cleanly.
    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield sse_done()
