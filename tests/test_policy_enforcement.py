"""Tests for backend policy enforcement: routing, concurrency, and payload policies."""

import asyncio
import os
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, Mock, patch

# Ensure config is set before importing app modules
os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.backends import (
    BackendConfig,
    BackendRegistry,
    AdmissionController,
    init_backends,
    get_admission_controller,
    check_capability,
)


@pytest.fixture
def test_registry():
    """Create a test backend registry."""
    backends = {
        "local_mlx": BackendConfig(
            backend_class="local_mlx",
            base_url="http://127.0.0.1:10240/v1",
            description="Test MLX",
            supported_capabilities=["chat", "embeddings"],
            concurrency_limits={"chat": 2, "embeddings": 2},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        ),
        "gpu_fast": BackendConfig(
            backend_class="gpu_fast",
            base_url="http://ai1.local:11434",
            description="Test Fast GPU",
            supported_capabilities=["chat", "embeddings"],
            concurrency_limits={"chat": 4, "embeddings": 4},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        ),
        "gpu_heavy": BackendConfig(
            backend_class="gpu_heavy",
            base_url="http://ada2.local:7860",
            description="Test Heavy GPU",
            supported_capabilities=["images"],
            concurrency_limits={"images": 2},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={"images_format": "url", "images_allow_base64": True},
        ),
    }
    
    legacy_mapping = {
        "ollama": "gpu_fast",
        "mlx": "local_mlx",
    }
    
    return BackendRegistry(backends=backends, legacy_mapping=legacy_mapping)


@pytest.fixture
def admission_controller(test_registry):
    """Create a test admission controller."""
    return AdmissionController(test_registry)


class TestCapabilityGating:
    """Test that requests are rejected when backend doesn't support the capability."""
    
    @pytest.mark.asyncio
    async def test_chat_on_image_backend_fails(self, test_registry):
        """Chat requests to gpu_heavy should fail (only supports images)."""
        # Mock the global registry
        with patch("app.backends.get_registry", return_value=test_registry):
            with pytest.raises(HTTPException) as exc_info:
                await check_capability("gpu_heavy", "chat")
            
            assert exc_info.value.status_code == 400
            assert "capability_not_supported" in str(exc_info.value.detail)
    
    @pytest.mark.asyncio
    async def test_images_on_chat_backend_fails(self, test_registry):
        """Image requests to gpu_fast should fail (only supports chat)."""
        with patch("app.backends.get_registry", return_value=test_registry):
            with pytest.raises(HTTPException) as exc_info:
                await check_capability("gpu_fast", "images")
            
            assert exc_info.value.status_code == 400
            assert "capability_not_supported" in str(exc_info.value.detail)
    
    @pytest.mark.asyncio
    async def test_supported_capability_passes(self, test_registry):
        """Supported capabilities should pass without error."""
        with patch("app.backends.get_registry", return_value=test_registry):
            # Should not raise
            await check_capability("gpu_heavy", "images")
            await check_capability("gpu_fast", "chat")
            await check_capability("local_mlx", "embeddings")


class TestAdmissionControl:
    """Test concurrency enforcement with fast-fail 429 responses."""
    
    @pytest.mark.asyncio
    async def test_within_limit_succeeds(self, admission_controller):
        """Requests within concurrency limit should succeed."""
        # gpu_heavy.images has limit of 2
        await admission_controller.acquire("gpu_heavy", "images")
        await admission_controller.acquire("gpu_heavy", "images")
        
        # Clean up
        admission_controller.release("gpu_heavy", "images")
        admission_controller.release("gpu_heavy", "images")
    
    @pytest.mark.asyncio
    async def test_exceeding_limit_fails_with_429(self, admission_controller):
        """Requests exceeding limit should fail with 429."""
        # gpu_heavy.images has limit of 2
        await admission_controller.acquire("gpu_heavy", "images")
        await admission_controller.acquire("gpu_heavy", "images")
        
        # Third request should fail
        with pytest.raises(HTTPException) as exc_info:
            await admission_controller.acquire("gpu_heavy", "images")
        
        assert exc_info.value.status_code == 429
        assert "backend_overloaded" in str(exc_info.value.detail)
        assert exc_info.value.headers.get("Retry-After") == "5"
        
        # Clean up
        admission_controller.release("gpu_heavy", "images")
        admission_controller.release("gpu_heavy", "images")
    
    @pytest.mark.asyncio
    async def test_release_frees_slot(self, admission_controller):
        """Releasing a slot should allow new requests."""
        # Fill to capacity
        await admission_controller.acquire("gpu_heavy", "images")
        await admission_controller.acquire("gpu_heavy", "images")
        
        # Release one
        admission_controller.release("gpu_heavy", "images")
        
        # Should be able to acquire again
        await admission_controller.acquire("gpu_heavy", "images")
        
        # Clean up
        admission_controller.release("gpu_heavy", "images")
        admission_controller.release("gpu_heavy", "images")
    
    @pytest.mark.asyncio
    async def test_different_routes_independent(self, admission_controller):
        """Different route kinds should have independent limits."""
        # Fill gpu_fast.chat (limit 4)
        for _ in range(4):
            await admission_controller.acquire("gpu_fast", "chat")
        
        # gpu_fast.embeddings should still work (separate limit)
        await admission_controller.acquire("gpu_fast", "embeddings")
        
        # Clean up
        for _ in range(4):
            admission_controller.release("gpu_fast", "chat")
        admission_controller.release("gpu_fast", "embeddings")
    
    @pytest.mark.asyncio
    async def test_different_backends_independent(self, admission_controller):
        """Different backends should have independent limits."""
        # Fill local_mlx.chat (limit 2)
        await admission_controller.acquire("local_mlx", "chat")
        await admission_controller.acquire("local_mlx", "chat")
        
        # gpu_fast.chat should still work (different backend)
        await admission_controller.acquire("gpu_fast", "chat")
        
        # Clean up
        admission_controller.release("local_mlx", "chat")
        admission_controller.release("local_mlx", "chat")
        admission_controller.release("gpu_fast", "chat")
    
    def test_stats_tracking(self, admission_controller):
        """Admission controller should track statistics correctly."""
        stats = admission_controller.get_stats()
        
        # Should have entries for all backend/route combinations
        assert "gpu_heavy.images" in stats
        assert "gpu_fast.chat" in stats
        assert "local_mlx.embeddings" in stats
        
        # Check structure
        gpu_heavy_images = stats["gpu_heavy.images"]
        assert gpu_heavy_images["limit"] == 2
        assert gpu_heavy_images["available"] == 2
        assert gpu_heavy_images["inflight"] == 0


class TestImagePayloadPolicy:
    """Test that images default to URL and only allow base64 when requested."""
    
    @pytest.mark.asyncio
    async def test_default_response_format_is_url(self):
        """Default response_format should be 'url'."""
        from app.images_backend import generate_images
        
        # Mock the backend
        with patch("app.images_backend.S.IMAGES_BACKEND", "mock"):
            result = await generate_images(
                prompt="test",
                size="512x512",
                n=1,
            )
        
        # Default should return URLs, not base64
        assert "data" in result
        for item in result["data"]:
            assert "url" in item
            assert "b64_json" not in item
    
    @pytest.mark.asyncio
    async def test_explicit_base64_allowed(self):
        """Explicit response_format=b64_json should work."""
        from app.images_backend import generate_images
        
        with patch("app.images_backend.S.IMAGES_BACKEND", "mock"):
            result = await generate_images(
                prompt="test",
                size="512x512",
                n=1,
                response_format="b64_json",
            )
        
        # Should return base64
        assert "data" in result
        for item in result["data"]:
            assert "b64_json" in item
            assert "url" not in item
    
    @pytest.mark.asyncio
    async def test_invalid_response_format_rejected(self):
        """Invalid response_format should be rejected."""
        from app.images_backend import generate_images
        
        with patch("app.images_backend.S.IMAGES_BACKEND", "mock"):
            with pytest.raises(ValueError) as exc_info:
                await generate_images(
                    prompt="test",
                    size="512x512",
                    n=1,
                    response_format="invalid",
                )
            
            assert "response_format must be" in str(exc_info.value)


class TestDeterministicRouting:
    """Test that routing is deterministic and doesn't auto-fallback."""
    
    def test_legacy_mapping_resolved(self, test_registry):
        """Legacy backend names should resolve to correct classes."""
        assert test_registry.resolve_backend_class("ollama") == "gpu_fast"
        assert test_registry.resolve_backend_class("mlx") == "local_mlx"
        assert test_registry.resolve_backend_class("gpu_heavy") == "gpu_heavy"
    
    def test_backend_config_lookup(self, test_registry):
        """Backend config lookup should work with legacy names."""
        # Direct lookup
        config = test_registry.get_backend("gpu_heavy")
        assert config is not None
        assert config.backend_class == "gpu_heavy"
        
        # Legacy lookup
        config = test_registry.get_backend("ollama")
        assert config is not None
        assert config.backend_class == "gpu_fast"


class TestHealthChecks:
    """Test backend health and readiness checking."""
    
    @pytest.mark.asyncio
    async def test_health_check_marks_unhealthy_backend(self):
        """Unhealthy backends should be marked as not ready."""
        from app.health_checker import HealthChecker
        
        checker = HealthChecker(check_interval=999, timeout=1.0)
        
        # Mock httpx to return 503
        with patch("app.health_checker.httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 503
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            # Manually trigger check
            config = BackendConfig(
                backend_class="test",
                base_url="http://test",
                description="Test",
                supported_capabilities=["chat"],
                concurrency_limits={"chat": 1},
                health_liveness="/healthz",
                health_readiness="/readyz",
                payload_policy={},
            )
            
            await checker._check_backend("test", config)
        
        status = checker.get_status("test")
        assert status is not None
        assert not status.is_ready
    
    def test_optimistic_start_no_checks_yet(self):
        """Before any checks, backends should be assumed ready."""
        from app.health_checker import HealthChecker
        
        checker = HealthChecker()
        
        # No checks run yet
        assert checker.is_ready("nonexistent_backend") is True
