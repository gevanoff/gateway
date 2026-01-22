import json
from pathlib import Path

import pytest
import httpx

AUTH_HEADERS = {"authorization": "Bearer test-token"}


def _fake_route(*, backend: str = "ollama", model: str = "qwen2.5:0", reason: str = "test"):
    class _R:
        def __init__(self):
            self.backend = backend
            self.model = model
            self.reason = reason

    return _R()


@pytest.mark.asyncio
async def test_agent_runs_heartmula_tool(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    # Persist runs to temp.
    import app.config as config

    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(config.S, "AGENT_RUNS_LOG_MODE", "per_run")

    # Ensure tool bus allows heartmula_generate
    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"heartmula_generate"})
    # Point tools registry to our bundled registry JSON so heartmula_generate is declared
    import app.config as config
    monkeypatch.setattr(config.S, "TOOLS_REGISTRY_PATH", str(Path(__file__).resolve().parents[1] / "app" / "tools_registry.json"))

    # Deterministic routing.
    import app.agent_runtime_v1 as agent_rt

    monkeypatch.setattr(agent_rt, "decide_route", lambda **_kw: _fake_route(backend="ollama", model="test-upstream"))
    # Allow heartmula_generate in the agent tools_for_tier
    monkeypatch.setattr(agent_rt, "tools_for_tier", lambda tier: {"heartmula_generate"})

    calls = {"n": 0}

    async def _fake_call_ollama(req, model_name):
        calls["n"] += 1
        # 1: plan, 2: action (tool), 3: plan, 4: action (final)
        if calls["n"] == 1:
            msg = {"role": "assistant", "content": "PLAN: use heartmula"}
        elif calls["n"] == 2:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "heartmula_generate", "arguments": json.dumps({"prompt": "x", "duration": 5})},
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

    # Mock run_tool_call on the imported agent runtime (it imports run_tool_call directly)
    monkeypatch.setattr(agent_rt, "run_tool_call", lambda name, arguments_json, *, allowed_tools=None: {"ok": True, "audio_url": "/audio/gen-1.wav"})

    from app import agent_routes

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
    events = payload.get("events")
    assert isinstance(events, list)

    # Find tool event and assert it contains audio_url
    tool_events = [e for e in events if e.get("type") == "tool" and e.get("name") == "heartmula_generate"]
    assert len(tool_events) == 1
    res = tool_events[0].get("result")
    assert isinstance(res, dict)
    assert res.get("audio_url") == "/audio/gen-1.wav"

    # The run replay should include the same tool result
    run_id = payload.get("run_id")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r2 = await client.get(f"/v1/agent/replay/{run_id}", headers=AUTH_HEADERS)
        assert r2.status_code == 200
        rep = r2.json()
        events2 = rep.get("events")
        tool_events2 = [e for e in events2 if e.get("type") == "tool" and e.get("name") == "heartmula_generate"]
        assert len(tool_events2) == 1
        assert tool_events2[0].get("result", {}).get("audio_url") == "/audio/gen-1.wav"
