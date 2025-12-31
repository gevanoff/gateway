import httpx
import pytest


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
            headers={"authorization": "Bearer test-token"},
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
            headers={"authorization": "Bearer test-token"},
        )
        assert r2.status_code == 400


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
            headers={"authorization": "Bearer test-token"},
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True
        assert j["echo"] == "hello"
        assert "replay_id" in j
