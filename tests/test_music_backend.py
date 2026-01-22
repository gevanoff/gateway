import pytest
import httpx
import asyncio

from app.config import S
from app.music_backend import generate_music
from app.audio_routes import proxy_heartmula_audio


@pytest.mark.asyncio
async def test_generate_music_transforms_audio_url(monkeypatch):
    # Configure a fake upstream base URL
    S.HEARTMULA_BASE_URL = "http://heartmula.local:9920"

    # Fake response from HeartMula
    class FakeResponse:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    async def mock_post(self, url, json=None):
        # The effective base may come from backends registry; ensure path is present
        assert url.endswith("/v1/music/generations")
        return FakeResponse({"id": "gen-1", "audio_url": "/audio/gen-1.wav"})

    # Force the backend base resolution to use our configured S value
    monkeypatch.setattr("app.music_backend._effective_heartmula_base_url", lambda backend_class: S.HEARTMULA_BASE_URL)
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post, raising=True)

    out = await generate_music(backend_class="heartmula_music", body={"prompt": "test music", "duration": 1})

    assert "audio_url" in out
    assert out["audio_url"].startswith("/ui/heartmula/audio/")
    assert out.get("_gateway", {}).get("upstream_audio_url") is not None


@pytest.mark.asyncio
async def test_proxy_heartmula_audio(monkeypatch):
    S.HEARTMULA_BASE_URL = "http://heartmula.local:9920"

    filename = "test.wav"
    expected_bytes = b"RIFF...."

    class FakeResp:
        status_code = 200
        headers = {"content-type": "audio/wav"}

        def __init__(self, content_bytes):
            self._content = content_bytes

        async def aiter_bytes(self):
            # Async generator yielding the content
            yield self._content

    async def mock_get(self, url, timeout=None):
        assert url.endswith(f"/audio/{filename}")
        return FakeResp(expected_bytes)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get, raising=True)

    # call the route function directly
    resp = await proxy_heartmula_audio(filename)

    # resp is a StreamingResponse - read bytes from it
    body = b""
    async for chunk in resp.body_iterator:
        body += chunk

    assert expected_bytes in body
    assert resp.media_type == "audio/wav"
