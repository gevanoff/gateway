import httpx
import pytest


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_bearer_and_returns_text(monkeypatch):
    from app.main import app

    import app.auth as auth

    # allow the request
    monkeypatch.setattr(auth, "require_bearer", lambda _req: None)

    import app.config as config

    monkeypatch.setattr(config.S, "METRICS_ENABLED", True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/metrics", headers={"authorization": "Bearer test-token"})
        assert r.status_code == 200
        assert "gateway_requests_total" in r.text
