import os


# The app config requires a bearer token at import time. Provide a test-only
# default so local/CI runs don't need a real secret.
os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

# Avoid tests making real network calls for embeddings/memory.
os.environ.setdefault("MEMORY_ENABLED", "false")
os.environ.setdefault("MEMORY_V2_ENABLED", "false")
