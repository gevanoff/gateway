from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/var/lib/gateway/app/.env", extra="ignore")

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    MLX_BASE_URL: str = "http://127.0.0.1:10240/v1"

    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8800
    GATEWAY_BEARER_TOKEN: str

    DEFAULT_BACKEND: Literal["ollama", "mlx"] = "ollama"

    # Backends can each have "strong" and "fast" model choices.
    OLLAMA_MODEL_STRONG: str = "qwen2.5:32b"
    OLLAMA_MODEL_FAST: str = "qwen2.5:7b"
    MLX_MODEL_STRONG: str = "mlx-community/gemma-2-2b-it-8bit"
    MLX_MODEL_FAST: str = "mlx-community/gemma-2-2b-it-8bit"

    # Legacy aliases kept for backward compatibility
    OLLAMA_MODEL_DEFAULT: str = "qwen2.5:32b"
    MLX_MODEL_DEFAULT: str = "mlx-community/gemma-2-2b-it-8bit"

    ROUTER_LONG_CONTEXT_CHARS: int = 40_000

    # Model alias registry (JSON via env, or JSON file on disk)
    # Example env:
    #   MODEL_ALIASES_JSON='{"aliases":{"coder":{"backend":"ollama","model":"deepseek-coder:33b"}}}'
    MODEL_ALIASES_JSON: str = ""
    MODEL_ALIASES_PATH: str = "/var/lib/gateway/app/model_aliases.json"

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_ALLOW_HTTP_FETCH: bool = False

    TOOLS_ALLOW_GIT: bool = False

    # Optional explicit allowlist; if set, only these tools may be executed.
    # Example: "read_file,write_file,http_fetch"
    TOOLS_ALLOWLIST: str = ""

    TOOLS_SHELL_CWD: str = "/var/lib/gateway/tools"
    TOOLS_SHELL_TIMEOUT_SEC: int = 20
    TOOLS_SHELL_ALLOWED_CMDS: str = ""  # comma-separated, e.g. "git,rg,ls,cat"

    TOOLS_FS_ROOTS: str = "/var/lib/gateway"  # comma-separated roots
    TOOLS_FS_MAX_BYTES: int = 200_000
    TOOLS_ALLOW_FS_WRITE: bool = False

    TOOLS_HTTP_ALLOWED_HOSTS: str = "127.0.0.1,localhost"
    TOOLS_HTTP_TIMEOUT_SEC: int = 10
    TOOLS_HTTP_MAX_BYTES: int = 200_000

    TOOLS_GIT_CWD: str = "/var/lib/gateway"
    TOOLS_GIT_TIMEOUT_SEC: int = 20

    EMBEDDINGS_BACKEND: Literal["ollama", "mlx"] = "ollama"
    EMBEDDINGS_MODEL: str = "nomic-embed-text"

    MEMORY_ENABLED: bool = True
    MEMORY_DB_PATH: str = "/var/lib/gateway/data/memory.sqlite"
    MEMORY_TOP_K: int = 6
    MEMORY_MIN_SIM: float = 0.25
    MEMORY_MAX_CHARS: int = 6000

    MEMORY_V2_ENABLED: bool = True
    MEMORY_V2_MAX_AGE_SEC: int = 60 * 60 * 24 * 30
    MEMORY_V2_TYPES_DEFAULT: str = "fact,preference,project"


S = Settings()

logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper())
