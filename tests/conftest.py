import os


# The app config requires a bearer token at import time. Provide a test-only
# default so local/CI runs don't need a real secret.
os.environ["GATEWAY_BEARER_TOKEN"] = "test-token"

# Avoid tests making real network calls for embeddings/memory.
os.environ["MEMORY_ENABLED"] = "false"
os.environ["MEMORY_V2_ENABLED"] = "false"
