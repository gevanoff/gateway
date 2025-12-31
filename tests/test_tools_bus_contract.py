import json

import httpx
import pytest


@pytest.mark.asyncio
async def test_tools_registry_subprocess_tool(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    # Registry declares a subprocess tool. Use the current Python executable for portability.
    py = __import__("sys").executable
    tool_name = "py_echo"
    registry_path = tmp_path / "tools_registry.json"
    registry_path.write_text(
        __import__("json").dumps(
            {
                "tools": [
                    {
                        "name": tool_name,
                        "version": "1",
                        "description": "Echo via python subprocess",
                        "parameters": {
                            "type": "object",
                            "properties": {"msg": {"type": "string"}},
                            "required": ["msg"],
                            "additionalProperties": False,
                        },
                        "exec": {
                            "type": "subprocess",
                            "argv": [
                                py,
                                "-c",
                                "import sys,json; a=json.load(sys.stdin); print(json.dumps({'echo':a.get('msg')}))",
                            ],
                            "timeout_sec": 5,
                        },
                    }
                ]
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    log_path = tmp_path / "tools_bus.jsonl"
    monkeypatch.setattr(tools_bus.S, "TOOLS_LOG_PATH", str(log_path))
    monkeypatch.setattr(tools_bus.S, "TOOLS_REGISTRY_PATH", str(registry_path))
    monkeypatch.setattr(tools_bus.S, "TOOLS_ALLOWLIST", tool_name)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Should appear in listing.
        r_list = await client.get("/v1/tools", headers=_auth_headers())
        assert r_list.status_code == 200
        names = {t.get("name") for t in r_list.json().get("data", [])}
        assert tool_name in names

        r = await client.post(
            f"/v1/tools/{tool_name}",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j.get("ok") is True
        assert j.get("exit_code") == 0
        assert isinstance(j.get("stdout"), str) and "hi" in j.get("stdout")
        assert j.get("stdout_json") == {"echo": "hi"}
        assert "replay_id" in j
        assert "request_hash" in j

        # Same args => stable request_hash
        r2 = await client.post(
            f"/v1/tools/{tool_name}",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r2.status_code == 200
        assert r2.json()["request_hash"] == j["request_hash"]

    assert log_path.exists()
    events = _read_jsonl(log_path)
    assert any(e.get("tool") == tool_name and e.get("version") == "1" for e in events)


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
async def test_tools_per_invocation_log_writes_file(monkeypatch, tmp_path):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus.S, "TOOLS_LOG_MODE", "per_invocation")
    monkeypatch.setattr(tools_bus.S, "TOOLS_LOG_DIR", str(tmp_path))

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo"})
    tools_bus.TOOL_SCHEMAS["echo"] = {
        "name": "echo",
        "version": "1",
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
            json={"arguments": {"msg": "hello"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        replay_id = r.json().get("replay_id")
        assert replay_id

    p = tmp_path / f"{replay_id}.json"
    assert p.exists()
    event = json.loads(p.read_text(encoding="utf-8").strip())
    assert event["replay_id"] == replay_id
    assert event["tool"] == "echo"

    # Replay endpoint should return the same event.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_rep = await client.get(f"/v1/tools/replay/{replay_id}", headers=_auth_headers())
        assert r_rep.status_code == 200
        rep = r_rep.json()
        assert rep.get("replay_id") == replay_id
        assert rep.get("tool") == "echo"


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
        "version": "1",
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
        assert "request_hash" in j and isinstance(j["request_hash"], str) and len(j["request_hash"]) == 64
        assert "tool_runtime_ms" in j
        assert j.get("tool_cpu_ms") is None or isinstance(j.get("tool_cpu_ms"), (int, float))
        assert isinstance(j.get("tool_io_bytes"), (int, float))
        assert j["ok"] is True
        assert j["echo"] == "hi"

        # Same call => same request_hash (replay_id differs)
        r_same = await client.post(
            "/v1/tools/echo",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r_same.status_code == 200
        j_same = r_same.json()
        assert j_same["request_hash"] == j["request_hash"]

        # Invalid: unexpected field
        r2 = await client.post(
            "/v1/tools/echo",
            json={"arguments": {"msg": "hi", "extra": 1}},
            headers=_auth_headers(),
        )
        assert r2.status_code == 400
        detail = r2.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("error_type") == "invalid_arguments"
        assert isinstance(detail.get("issues"), list) and detail.get("issues")

        # Unknown tool (not allowlisted) should be 404 with normalized envelope.
        r3 = await client.post(
            "/v1/tools/definitely_not_allowed",
            json={"arguments": {}},
            headers=_auth_headers(),
        )
        assert r3.status_code == 404
        d3 = r3.json().get("detail")
        assert isinstance(d3, dict)
        assert d3.get("error_type") == "unknown_tool"


@pytest.mark.asyncio
async def test_tools_undeclared_allowlisted_tool_returns_404_envelope(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    name = "definitely_missing_tool_xyz"
    assert name not in tools_bus.TOOL_SCHEMAS

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {name})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/v1/tools/{name}",
            json={"arguments": {}},
            headers=_auth_headers(),
        )
        assert r.status_code == 404
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("error_type") == "undeclared_tool"


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
        "version": "1",
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
    assert isinstance(last.get("request_hash"), str) and len(last.get("request_hash")) == 64
    assert last.get("tool") == "echo"
    assert last.get("version") == "1"
    assert last.get("ok") is True
    assert "tool_runtime_ms" in last
    assert last.get("tool_cpu_ms") is None or isinstance(last.get("tool_cpu_ms"), (int, float))
    assert isinstance(last.get("tool_io_bytes"), (int, float))


@pytest.mark.asyncio
async def test_tools_dispatch_endpoint(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo"})
    tools_bus.TOOL_SCHEMAS["echo"] = {
        "name": "echo",
        "version": "1",
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
        assert "request_hash" in j
        assert "tool_runtime_ms" in j
        assert "tool_cpu_ms" in j
        assert "tool_io_bytes" in j

        r_bad_body = await client.post(
            "/v1/tools",
            json=["not", "an", "object"],
            headers=_auth_headers(),
        )
        assert r_bad_body.status_code == 400
        d1 = r_bad_body.json().get("detail")
        assert isinstance(d1, dict)
        assert d1.get("error_type") == "invalid_request"

        r_bad_name = await client.post(
            "/v1/tools",
            json={"name": "", "arguments": {}},
            headers=_auth_headers(),
        )
        assert r_bad_name.status_code == 400
        d2 = r_bad_name.json().get("detail")
        assert isinstance(d2, dict)
        assert d2.get("error_type") == "invalid_request"

        r_bad_args = await client.post(
            "/v1/tools",
            json={"name": "echo", "arguments": "nope"},
            headers=_auth_headers(),
        )
        assert r_bad_args.status_code == 400
        d3 = r_bad_args.json().get("detail")
        assert isinstance(d3, dict)
        assert d3.get("error_type") == "invalid_request"


@pytest.mark.asyncio
async def test_tools_parses_stdout_json_for_native_tool(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"echo_stdout"})
    tools_bus.TOOL_SCHEMAS["echo_stdout"] = {
        "name": "echo_stdout",
        "version": "1",
        "description": "Echo via stdout JSON",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        },
    }

    def _impl(args):
        import json as _json

        return {"ok": True, "stdout": _json.dumps({"echo": args.get("msg")}), "stderr": ""}

    tools_bus.TOOL_IMPL["echo_stdout"] = _impl

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/tools/echo_stdout",
            json={"arguments": {"msg": "hi"}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j.get("ok") is True
        assert j.get("stdout_json") == {"echo": "hi"}

@pytest.mark.asyncio
async def test_tools_error_envelope_has_type_and_message(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.tools_bus as tools_bus

    monkeypatch.setattr(tools_bus, "_allowed_tool_names", lambda: {"boom"})
    tools_bus.TOOL_SCHEMAS["boom"] = {
        "name": "boom",
        "version": "1",
        "description": "Raise an exception",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }

    def _impl(_args):
        raise ValueError("nope")

    tools_bus.TOOL_IMPL["boom"] = _impl

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/tools/boom",
            json={"arguments": {}},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j.get("ok") is False
        assert j.get("error_type") == "ValueError"
        assert "nope" in (j.get("error_message") or "")
