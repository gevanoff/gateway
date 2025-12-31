import json

import httpx
import pytest


def _read_jsonl(path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _auth_headers() -> dict:
    import os

    token = os.environ.get("GATEWAY_BEARER_TOKEN", "test-token")
    return {"authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_request_log_writes_nonstream_event(monkeypatch, tmp_path):
    from app.main import app

    import app.tools_bus as tools_bus

    # Keep /v1/tools deterministic and allowed.
    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: set())

    # Redirect request logs to temp.
    log_path = tmp_path / "requests.jsonl"
    monkeypatch.setattr(tools_bus.S, "REQUEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(tools_bus.S, "REQUEST_LOG_ENABLED", True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/v1/tools", headers=_auth_headers())
        assert r.status_code == 200
        req_id = r.headers.get("x-request-id")
        assert isinstance(req_id, str) and req_id.startswith("req-")

    assert log_path.exists()
    events = _read_jsonl(log_path)
    ev = next(e for e in events if e.get("path") == "/v1/tools" and e.get("stream") is False)
    assert ev.get("request_id") == req_id


@pytest.mark.asyncio
async def test_request_log_writes_stream_metrics(monkeypatch, tmp_path):
    from app.main import app

    import app.openai_routes as openai_routes
    import app.config as config
    import app.request_log as request_log

    # Redirect request logs to temp.
    log_path = tmp_path / "requests.jsonl"
    monkeypatch.setattr(config.S, "REQUEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(config.S, "REQUEST_LOG_ENABLED", True)
    monkeypatch.setattr(request_log.S, "REQUEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(request_log.S, "REQUEST_LOG_ENABLED", True)

    # Deterministic routing.
    class _Route:
        backend = "ollama"
        model = "qwen2.5:7b"
        reason = "test"

    monkeypatch.setattr(openai_routes, "decide_route", lambda **_kw: _Route())

    # Deterministic stream.
    from app.openai_utils import sse, sse_done

    async def _fake_stream(*_a, **_kw):
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
                "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
            }
        )
        yield sse_done()

    monkeypatch.setattr(openai_routes, "stream_ollama_chat_as_openai", _fake_stream)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={**_auth_headers(), "accept": "text/event-stream"},
        )
        assert r.status_code == 200
        req_id = r.headers.get("x-request-id")
        assert isinstance(req_id, str) and req_id.startswith("req-")
        # Consume stream so middleware can finalize and log.
        _ = r.content

    assert log_path.exists()
    events = _read_jsonl(log_path)
    stream_events = [
        e
        for e in events
        if e.get("path") == "/v1/chat/completions" and e.get("stream") is True
    ]
    assert stream_events, events
    ev = stream_events[-1]
    assert ev.get("request_id") == req_id
    assert ev.get("chunks_out", 0) >= 1
    assert ev.get("bytes_out", 0) > 0
    assert isinstance(ev.get("ttft_ms"), (int, float))
