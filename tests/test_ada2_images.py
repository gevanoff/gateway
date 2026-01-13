"""Integration tests for ada2 image generation backend.

These tests verify:
1. Image generation routes to gpu_heavy (ada2.local)
2. URL responses are returned by default
3. base64 responses work with explicit request
4. Admission control enforces concurrency limits (429 on overload)
5. Health checks gate requests (503 when backend not ready)
6. No fallback to other backends (fail fast)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from typing import Any, Dict

import httpx
import pytest

from app.backends import BackendRegistry, get_backends
from app.config import S, logger
from app.health_checker import HealthChecker, get_health_checker


@pytest.fixture
def mock_ada2_backend(monkeypatch):
    """Mock the ada2 backend to return test images."""
    
    async def mock_generate(prompt: str, **kwargs) -> Dict[str, Any]:
        """Return a mock SVG image."""
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">'
            f'<rect width="512" height="512" fill="#3498db"/>'
            f'<text x="256" y="256" text-anchor="middle" fill="white" font-size="20">'
            f'{prompt[:50]}</text></svg>'
        )
        b64_data = base64.b64encode(svg.encode()).decode()
        
        # Simulate processing time
        await asyncio.sleep(0.5)
        
        return {
            "created": int(time.time()),
            "data": [{"b64_json": b64_data}],
            "_gateway": {"mime": "image/svg+xml"}
        }
    
    # Patch the http_openai_images backend
    from app import images_backend
    monkeypatch.setattr(images_backend, "_generate_http_openai_images", mock_generate)


@pytest.mark.asyncio
async def test_routes_to_gpu_heavy_ada2(client, mock_ada2_backend):
    """Verify image requests route to gpu_heavy (ada2.local)."""
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "a red apple on a table",
            "size": "512x512",
            "n": 1,
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Check routing headers
    assert response.headers.get("X-Backend-Used") == "gpu_heavy"
    assert response.headers.get("X-Router-Reason") == "backend_class"
    
    # Check response structure
    assert "data" in data
    assert len(data["data"]) == 1


@pytest.mark.asyncio
async def test_returns_url_by_default(client, mock_ada2_backend):
    """Verify URL response format is default."""
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "test image for URL",
            "size": "512x512",
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should have URL, not b64_json
    assert "data" in data
    image = data["data"][0]
    assert "url" in image
    assert image["url"].startswith("/ui/images/")
    assert "b64_json" not in image


@pytest.mark.asyncio
async def test_returns_base64_when_requested(client, mock_ada2_backend, monkeypatch):
    """Verify base64 response when explicitly requested."""
    
    # Disable URL conversion for this test
    from app import images_routes
    monkeypatch.setattr(images_routes, "convert_response_to_urls", lambda x: x)
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "test image for base64",
            "size": "512x512",
            "response_format": "b64_json",
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should have b64_json
    image = data["data"][0]
    assert "b64_json" in image
    assert isinstance(image["b64_json"], str)
    
    # Verify it's valid base64
    try:
        decoded = base64.b64decode(image["b64_json"])
        assert len(decoded) > 0
    except Exception as e:
        pytest.fail(f"Invalid base64 data: {e}")


@pytest.mark.asyncio
async def test_enforces_concurrency_limit(client, mock_ada2_backend):
    """Verify 429 when exceeding gpu_heavy.images limit (2)."""
    
    # Send 3 requests simultaneously (limit is 2)
    tasks = []
    for i in range(3):
        task = client.post(
            "/v1/images/generations",
            json={
                "prompt": f"test image {i}",
                "size": "512x512",
            }
        )
        tasks.append(task)
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    # At least one should be 429
    status_codes = [r.status_code if hasattr(r, "status_code") else 500 for r in responses]
    assert 429 in status_codes, f"Expected 429, got: {status_codes}"
    
    # 429 response should have Retry-After header
    for resp in responses:
        if hasattr(resp, "status_code") and resp.status_code == 429:
            assert "Retry-After" in resp.headers
            retry_after = int(resp.headers["Retry-After"])
            assert retry_after >= 1


@pytest.mark.asyncio
async def test_fails_when_backend_not_ready(client, mock_ada2_backend):
    """Verify 503 when gpu_heavy is not ready."""
    
    # Mark gpu_heavy as not ready
    health_checker = get_health_checker()
    health_checker._backend_health["gpu_heavy"]["ready"] = False
    health_checker._backend_health["gpu_heavy"]["error"] = "Connection refused"
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "test image",
            "size": "512x512",
        }
    )
    
    assert response.status_code == 503
    data = response.json()
    assert "detail" in data
    assert "not ready" in data["detail"].lower()
    
    # Restore ready state
    health_checker._backend_health["gpu_heavy"]["ready"] = True
    health_checker._backend_health["gpu_heavy"]["error"] = None


@pytest.mark.asyncio
async def test_no_fallback_to_other_backends(client):
    """Verify images ONLY route to gpu_heavy, no fallback."""
    
    # Mark gpu_heavy as unhealthy
    health_checker = get_health_checker()
    original_healthy = health_checker._backend_health["gpu_heavy"]["healthy"]
    health_checker._backend_health["gpu_heavy"]["healthy"] = False
    health_checker._backend_health["gpu_heavy"]["error"] = "Simulated failure"
    
    try:
        response = await client.post(
            "/v1/images/generations",
            json={
                "prompt": "test image",
                "size": "512x512",
            }
        )
        
        # Should fail (503), not fall back to local_mlx or gpu_fast
        assert response.status_code == 503
        data = response.json()
        
        # Should NOT have routed to another backend
        backend_used = response.headers.get("X-Backend-Used")
        assert backend_used != "local_mlx", "Should not fall back to local_mlx"
        assert backend_used != "gpu_fast", "Should not fall back to gpu_fast"
        
    finally:
        # Restore healthy state
        health_checker._backend_health["gpu_heavy"]["healthy"] = original_healthy
        health_checker._backend_health["gpu_heavy"]["error"] = None


@pytest.mark.asyncio
async def test_stores_images_content_addressed(client, mock_ada2_backend, tmp_path):
    """Verify images are stored with content-addressed filenames."""
    
    # Set a temporary image directory
    import app.image_storage as img_storage
    original_dir = img_storage.S.UI_IMAGE_DIR
    test_dir = tmp_path / "ui_images"
    test_dir.mkdir()
    img_storage.S.UI_IMAGE_DIR = str(test_dir)
    
    try:
        # Generate an image
        response = await client.post(
            "/v1/images/generations",
            json={
                "prompt": "test content addressing",
                "size": "512x512",
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        url = data["data"][0]["url"]
        
        # Extract filename
        filename = url.split("/")[-1]
        assert "_" in filename  # Format: {timestamp}_{hash}.ext
        
        # Verify file exists
        image_path = test_dir / filename
        assert image_path.exists()
        
        # Verify hash matches content
        content = image_path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()[:16]
        assert content_hash in filename
        
    finally:
        # Restore original directory
        img_storage.S.UI_IMAGE_DIR = original_dir


@pytest.mark.asyncio
async def test_large_image_generation(client, mock_ada2_backend):
    """Verify large images (1024x1024) work correctly."""
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "a detailed landscape painting",
            "size": "1024x1024",
            "n": 1,
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify response
    image = data["data"][0]
    assert "url" in image
    
    # Verify routing headers
    assert response.headers.get("X-Backend-Used") == "gpu_heavy"


@pytest.mark.asyncio
async def test_batch_generation_respects_limits(client, mock_ada2_backend):
    """Verify batch generation (n>1) respects concurrency limits."""
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "multiple test images",
            "size": "512x512",
            "n": 2,  # Request 2 images
        }
    )
    
    # Should succeed (each request counts as one slot)
    assert response.status_code == 200
    data = response.json()
    
    # Should return 2 images
    assert len(data["data"]) == 2
    for image in data["data"]:
        assert "url" in image


@pytest.mark.asyncio
async def test_error_handling_from_backend(client, monkeypatch):
    """Verify proper error handling when backend fails."""
    
    async def mock_generate_error(*args, **kwargs):
        raise RuntimeError("Model not loaded")
    
    from app import images_backend
    monkeypatch.setattr(images_backend, "_generate_http_openai_images", mock_generate_error)
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "test error",
            "size": "512x512",
        }
    )
    
    # Should return 5xx error
    assert response.status_code >= 500
    data = response.json()
    assert "detail" in data


@pytest.mark.asyncio
async def test_nexa_not_used_for_images(client):
    """Verify local_mlx (Nexa) is never used for image generation."""
    
    # Check backends config
    backends = get_backends()
    local_mlx = backends.get_backend("local_mlx")
    
    # local_mlx should NOT have images capability
    assert "images" not in local_mlx.capabilities
    
    # Verify routing doesn't select local_mlx for images
    from app.router import decide_route
    route = decide_route(
        model=None,
        backend_header=None,
        request_type="images",
        tools=None,
        context_chars=0,
    )
    
    # Should route to gpu_heavy, not local_mlx
    assert route["backend"] != "local_mlx"
    assert route["backend"] == "gpu_heavy"


@pytest.mark.asyncio
async def test_health_status_reflects_ada2(client):
    """Verify /v1/gateway/status shows gpu_heavy health."""
    
    response = await client.get("/v1/gateway/status")
    assert response.status_code == 200
    
    data = response.json()
    
    # Check backend health section
    assert "backend_health" in data
    assert "gpu_heavy" in data["backend_health"]
    
    gpu_heavy_health = data["backend_health"]["gpu_heavy"]
    assert "healthy" in gpu_heavy_health
    assert "ready" in gpu_heavy_health
    
    # Check admission control
    assert "admission_control" in data
    assert "gpu_heavy.images" in data["admission_control"]
    
    images_admission = data["admission_control"]["gpu_heavy.images"]
    assert images_admission["limit"] == 2
    assert "available" in images_admission
    assert "inflight" in images_admission


@pytest.mark.asyncio
async def test_invalid_size_rejected(client, mock_ada2_backend):
    """Verify invalid image sizes are rejected."""
    
    response = await client.post(
        "/v1/images/generations",
        json={
            "prompt": "test",
            "size": "3000x3000",  # Exceeds IMAGES_MAX_PIXELS
        }
    )
    
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
