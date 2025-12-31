import httpx
import pytest


def _auth_headers() -> dict:
    token = __import__("os").environ.get("GATEWAY_BEARER_TOKEN", "test-token")
    return {"authorization": f"Bearer {token}"}


def _read_jsonl(path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines:
        if line.strip():
            out.append(__import__("json").loads(line))
    return out


@pytest.mark.asyncio
async def test_tools_exec_replay_id_and_schema_validation(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    # Restrict allowed tools to a single fake tool.
    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo"})
    tools_bus.TOOL_SCHEMAS["echo"] = {
        "name": "echo",
        "description": "Echo a string",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        },
    }

    tools_bus.TOOL_IMPL["echo"] = lambda args: {"ok": True, "echo": args.get("msg")}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Valid call
        r = await client.post(
            "/v1/tools/echo",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert "replay_id" in j and isinstance(j["replay_id"], str) and j["replay_id"]
        assert j["ok"] is True
        assert j["echo"] == "hi"

        # Invalid: unexpected field
        r2 = await client.post(
            "/v1/tools/echo",
            json={"arguments": {"msg": "hi", "extra": 1}},
            headers=_auth_headers(),
        )
        assert r2.status_code == 400


@pytest.mark.asyncio
async def test_tools_writes_jsonl_log_with_replay_id(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    log_path = tmp_path / "tools_bus.jsonl"
    monkeypatch.setattr(tools_bus.S, "TOOLS_LOG_PATH", str(log_path))

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo"})
    tools_bus.TOOL_SCHEMAS["echo"] = {
        "name": "echo",
        "description": "Echo a string",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        },
    }
    tools_bus.TOOL_IMPL["echo"] = lambda args: {"ok": True, "echo": args.get("msg")}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/tools/echo",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        replay_id = body.get("replay_id")
        assert isinstance(replay_id, str) and replay_id

    assert log_path.exists(), "expected tool bus to write a JSONL log file"
    events = _read_jsonl(log_path)
    assert events, "expected at least one log line"
    # Last event should match our call.
    last = events[-1]
    assert last.get("replay_id") == replay_id
    assert last.get("tool") == "echo"
    assert last.get("ok") is True


@pytest.mark.asyncio
async def test_tools_dispatch_endpoint(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo"})
    tools_bus.TOOL_SCHEMAS["echo"] = {
        "name": "echo",
        "description": "Echo a string",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        },
    }
    tools_bus.TOOL_IMPL["echo"] = lambda args: {"ok": True, "echo": args.get("msg")}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/tools",
            json={"name": "echo", "arguments": {"msg": "hello"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True
        assert j["echo"] == "hello"
        assert "replay_id" in j
