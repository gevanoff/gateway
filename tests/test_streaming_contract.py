import json
from typing import Any, AsyncIterator, Dict, List

import pytest
import httpx


@pytest.mark.asyncio
async def test_chat_completions_streaming_contract_golden(monkeypatch):
    """Golden contract for /v1/chat/completions streaming.

    Verifies:
    - Content-Type is text/event-stream
    - Proper SSE framing (data: ...\n\n)
    - At least one JSON event
    - Terminates with data: [DONE]\n\n
    This test is deterministic and does not require Ollama/MLX.
    """

    # Import inside the test so the app is built in the same process.
    from app.main import app

    # Patch bearer check so the test doesn't depend on env secrets.
    import app.auth as auth

    def _noop_require_bearer(_req):
        return None

    monkeypatch.setattr(auth, "require_bearer", _noop_require_bearer)

    # Patch router decision so we always pick the Ollama streaming path.
    import app.openai_routes as openai_routes

    class _Route:
        backend = "ollama"
        model = "qwen2.5:7b"
        reason = "test"

    monkeypatch.setattr(openai_routes, "decide_route", lambda **_kw: _Route())

    # Patch the upstream streaming generator to yield a deterministic sequence.
    import app.upstreams as upstreams
    from app.openai_utils import sse, sse_done

    async def _fake_stream_ollama_chat_as_openai(*_a, **_kw) -> AsyncIterator[bytes]:
        yield sse(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "ollama:qwen2.5:7b",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        yield sse(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "ollama:qwen2.5:7b",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
            }
        )
        yield sse(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "ollama:qwen2.5:7b",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield sse_done()

    monkeypatch.setattr(upstreams, "stream_ollama_chat_as_openai", _fake_stream_ollama_chat_as_openai)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "Bearer test", "accept": "text/event-stream"},
        )

        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/event-stream")

        raw = r.content
        assert raw.endswith(b"data: [DONE]\n\n")

        # Basic SSE framing assertions
        assert b"\n\ndata: " in raw or raw.startswith(b"data: ")

        # Parse each data frame; ignore [DONE]
        events: List[Dict[str, Any]] = []
        for block in raw.split(b"\n\n"):
            if not block:
                continue
            assert block.startswith(b"data: ")
            payload = block[len(b"data: ") :]
            if payload == b"[DONE]":
                continue
            events.append(json.loads(payload))

        assert events, "expected at least one JSON event"
        # Ensure at least one content delta exists
        assert any((((e.get("choices") or [{}])[0].get("delta") or {}).get("content") == "hello") for e in events)


@pytest.mark.asyncio
async def test_chat_completions_streaming_upstream_disconnect_yields_done(monkeypatch):
    """If the upstream generator dies early, the gateway should still end cleanly."""

    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.openai_routes as openai_routes

    class _Route:
        backend = "ollama"
        model = "qwen2.5:7b"
        reason = "test"

    monkeypatch.setattr(openai_routes, "decide_route", lambda **_kw: _Route())

    import app.upstreams as upstreams
    from app.openai_utils import sse

    async def _fake_stream_disconnect(*_a, **_kw) -> AsyncIterator[bytes]:
        # One chunk, then crash.
        yield sse(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "ollama:qwen2.5:7b",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        raise RuntimeError("upstream disconnected")

    monkeypatch.setattr(upstreams, "stream_ollama_chat_as_openai", _fake_stream_disconnect)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "Bearer test", "accept": "text/event-stream"},
        )

        # Still a 200 stream, but must terminate.
        assert r.status_code == 200
        raw = r.content
        assert raw.endswith(b"data: [DONE]\n\n"), raw[-200:]
