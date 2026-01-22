import json
import httpx
import pytest
from app.config import S


AUTH_HEADERS = {"authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_ui_api_music_calls_backend(monkeypatch):
    from app.main import app

    import app.auth as auth

    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    # Allow UI access for tests
    monkeypatch.setattr(S, "UI_IP_ALLOWLIST", "127.0.0.1")

    # Mock music backend
    async def _fake_generate_music(*, backend_class: str, body: dict):
        return {"audio_url": "/ui/heartmula/audio/test.wav", "_gateway": {"backend": "heartmula"}}

    monkeypatch.setattr("app.ui_routes.generate_music", _fake_generate_music, raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/ui/api/music", json={"prompt": "short tune", "duration": 5})
        assert r.status_code == 200
        data = r.json()
        assert data.get("audio_url") == "/ui/heartmula/audio/test.wav"
        assert isinstance(data.get("_gateway"), dict)


@pytest.mark.asyncio
async def test_ui_music_page_requires_allowlist(monkeypatch):
    from app.main import app
    import app.auth as auth
    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    # Do not set UI_IP_ALLOWLIST => access denied
    monkeypatch.setattr(S, "UI_IP_ALLOWLIST", "")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/ui/music")
        assert r.status_code == 403
