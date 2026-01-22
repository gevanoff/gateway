import httpx
import pytest

from app.config import S


@pytest.mark.asyncio
async def test_ui_topbar_includes_music_link(monkeypatch):
    from app.main import app

    import app.auth as auth
    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    # Allow UI access
    monkeypatch.setattr(S, "UI_IP_ALLOWLIST", "127.0.0.1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/ui", headers={})
        assert r.status_code == 200
        body = r.text
        # Ensure there's a link to /ui/music and the auto-music checkbox
        assert "/ui/music" in body
        assert "autoMusic" in body
