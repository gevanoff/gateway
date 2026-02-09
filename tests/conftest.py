import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# The app config requires a bearer token at import time. Provide a test-only
# default so local/CI runs don't need a real secret.
os.environ["GATEWAY_BEARER_TOKEN"] = "test-token"

# Avoid tests making real network calls for embeddings/memory.
os.environ["MEMORY_ENABLED"] = "false"
os.environ["MEMORY_V2_ENABLED"] = "false"


@pytest.fixture(scope="session", autouse=True)
def init_test_backends():
    """Initialize backends and health checker once for all tests."""
    from app.backends import init_backends
    from app.health_checker import init_health_checker
    
    init_backends()
    init_health_checker()
    # Don't start the background checker in tests
    
    yield
