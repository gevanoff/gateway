import json

import httpx
import pytest


AUTH_HEADERS = {"authorization": "Bearer test-token"}


def _fake_route(*, backend: str = "ollama", model: str = "qwen2.5:0", reason: str = "test"):
    class _R:
        def __init__(self):
            self.backend = backend
            self.model = model
            self.reason = reason

    return _R()


@pytest.mark.asyncio
async def test_agent_run_plan_tool_observe_terminate(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    # Persist runs to temp.
    import app.config as config

    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_MODE", "per_run")

    # Ensure tool bus has a deterministic tool.
    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"noop"})

    # Deterministic routing.
    import app.agent_runtime_v1 as agent_rt

    monkeypatch.setattr(agent_rt, "decide_route", lambda **_kw: _fake_route(backend="ollama", model="test-upstream"))

    # Fake upstream: plan step returns content, action step returns tool call then final.
    calls = {"n": 0}

    async def _fake_call_ollama(req, model_name):
        calls["n"] += 1
        # 1: plan, 2: action (tool), 3: plan, 4: action (final)
        if calls["n"] == 1:
            msg = {"role": "assistant", "content": "PLAN: use noop"}
        elif calls["n"] == 2:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "noop", "arguments": json.dumps({"text": "hi"})},
                    }
                ],
            }
        elif calls["n"] == 3:
            msg = {"role": "assistant", "content": "PLAN: answer"}
        else:
            msg = {"role": "assistant", "content": "FINAL: done"}

        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model_name,
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    monkeypatch.setattr(agent_rt, "call_ollama", _fake_call_ollama)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/agent/run",
            headers=AUTH_HEADERS,
            json={"agent": "default", "input": "hello"},
        )
        assert r.status_code == 200
        payload = r.json()

    assert payload.get("ok") is True
    assert payload.get("output_text") == "FINAL: done"
    assert isinstance(payload.get("run_id"), str) and payload["run_id"].startswith("run-")

    events = payload.get("events")
    assert isinstance(events, list)
    types = [e.get("type") for e in events]
    assert "run_started" in types
    assert "plan" in types
    assert "tool" in types
    assert types[-1] == "run_completed"

    # Replay should return the same persisted transcript.
    run_id = payload["run_id"]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r2 = await client.get(f"/v1/agent/replay/{run_id}", headers=AUTH_HEADERS)
        assert r2.status_code == 200
        rep = r2.json()
        assert rep.get("run_id") == run_id


@pytest.mark.asyncio
async def test_agent_admission_sheds_heavy_tiers(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.config as config

    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_MODE", "per_run")
    monkeypatch.setattr(config.S, "AGENT_SHED_HEAVY", True)

    # Force agent spec tier=2.
    import app.agent_runtime_v1 as agent_rt

    monkeypatch.setattr(
        agent_rt,
        "load_agent_specs",
        lambda: {"heavy": agent_rt.AgentSpecModel(model="fast", tier=2, max_turns=1)},
    )
    monkeypatch.setattr(agent_rt, "decide_route", lambda **_kw: _fake_route(backend="ollama", model="test-upstream"))

    async def _fake_call_ollama(req, model_name):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    monkeypatch.setattr(agent_rt, "call_ollama", _fake_call_ollama)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/agent/run",
            headers=AUTH_HEADERS,
            json={"agent": "heavy", "input": "hello"},
        )
        assert r.status_code == 429
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("error") == "shed_heavy"
