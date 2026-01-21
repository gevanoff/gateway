import os
import pytest
import pytest_asyncio
import httpx


# The app config requires a bearer token at import time. Provide a test-only
# default so local/CI runs don't need a real secret.
os.environ["GATEWAY_BEARER_TOKEN"] = "test-token"

# Avoid tests making real network calls for embeddings/memory.
os.environ["MEMORY_ENABLED"] = "false"
os.environ["MEMORY_V2_ENABLED"] = "false"

# Set a writable memory DB path for tests
os.environ.setdefault("MEMORY_DB_PATH", "/tmp/test_memory.db")


@pytest.fixture(scope="session", autouse=True)
def init_test_backends():
    """Initialize backends and health checker once for all tests."""
    from app.backends import init_backends
    from app.health_checker import init_health_checker
    
    init_backends()
    init_health_checker()
    # Don't start the background checker in tests
    
    yield


@pytest_asyncio.fixture
async def client():
    """Provide an async HTTP client for testing the FastAPI app."""
    from app.main import app
    
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
