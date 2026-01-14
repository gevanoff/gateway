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

    # Optional multi-token auth (comma-separated). When set, any listed token is accepted.
    # If empty, falls back to single-token GATEWAY_BEARER_TOKEN.
    GATEWAY_BEARER_TOKENS: str = ""

    # Optional per-token policy JSON. Format: {"<token>": { ...policy... }, ...}
    # Policy keys are best-effort and currently used for tool allowlists/rate limits.
    GATEWAY_TOKEN_POLICIES_JSON: str = ""

    # Optional request guardrails.
    # - MAX_REQUEST_BYTES: 0 disables. When enabled, requests exceeding this size return 413.
    # - IP_ALLOWLIST: comma-separated IPs and/or CIDRs (e.g. "127.0.0.1,10.0.0.0/8"). Empty allows all.
    MAX_REQUEST_BYTES: int = 1_000_000
    IP_ALLOWLIST: str = ""

    # Optional: restrict tokenless UI endpoints (/ui, /ui/api/*) to specific client IPs/CIDRs.
    # If empty, the UI endpoints are disabled (403) to avoid exposing unauthenticated access.
    UI_IP_ALLOWLIST: str = ""

    # Optional public base URL for constructing absolute URLs in API responses.
    # When set (e.g. "http://ai2:8800"), image responses that would otherwise return
    # relative paths like "/ui/images/<name>" can instead return fully-qualified URLs.
    # Leave empty to preserve relative URLs.
    PUBLIC_BASE_URL: str = ""

    # Tokenless UI image caching
    # The UI image endpoint can store generated images on disk and return short-lived URLs
    # served by the gateway (still gated by UI_IP_ALLOWLIST).
    UI_IMAGE_DIR: str = "/var/lib/gateway/data/ui_images"
    UI_IMAGE_TTL_SEC: int = 900
    UI_IMAGE_MAX_BYTES: int = 50_000_000

    # Images (text-to-image)
    # Default backend is "mock" which returns an SVG placeholder.
    # Set IMAGES_BACKEND=http_a1111 and IMAGES_HTTP_BASE_URL=http://127.0.0.1:7860 to use Automatic1111's API.
    # Set IMAGES_BACKEND=http_openai_images and IMAGES_HTTP_BASE_URL=http://127.0.0.1:18181 to use an OpenAI-style
    # image server (e.g., Nexa exposing POST /v1/images/generations).
    IMAGES_BACKEND: Literal["mock", "http_a1111", "http_openai_images"] = "mock"
    IMAGES_BACKEND_CLASS: str = "gpu_heavy"  # Backend class for routing/admission control
    IMAGES_HTTP_BASE_URL: str = "http://127.0.0.1:7860"
    IMAGES_HTTP_TIMEOUT_SEC: float = 120.0
    IMAGES_A1111_STEPS: int = 20
    IMAGES_MAX_PIXELS: int = 2_000_000
    IMAGES_OPENAI_MODEL: str = ""

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

    # If true, enable heuristic routing (tools/long-context/fast tier selection).
    # If false (default), routing is strictly alias/prefix/explicit-model driven.
    ROUTER_ENABLE_POLICY: bool = False

    # Model alias registry (JSON via env, or JSON file on disk)
    # Example env:
    #   MODEL_ALIASES_JSON='{"aliases":{"coder":{"backend":"ollama","model":"deepseek-coder:33b"}}}'
    MODEL_ALIASES_JSON: str = ""
    MODEL_ALIASES_PATH: str = "/var/lib/gateway/app/model_aliases.json"

    TOOLS_ALLOW_SHELL: bool = False
    TOOLS_ALLOW_FS: bool = False
    TOOLS_ALLOW_HTTP_FETCH: bool = False

    TOOLS_ALLOW_GIT: bool = False

    # Safe built-in tools (disabled by default; can be enabled or allowlisted).
    TOOLS_ALLOW_SYSTEM_INFO: bool = False
    TOOLS_ALLOW_MODELS_REFRESH: bool = False

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

    # Tool bus JSONL log file path.
    TOOLS_LOG_PATH: str = "/var/lib/gateway/data/tools/invocations.jsonl"

    # Tool invocation logging mode:
    # - ndjson: append-only JSONL at TOOLS_LOG_PATH
    # - per_invocation: one JSON file per replay_id under TOOLS_LOG_DIR
    # - both: do both
    TOOLS_LOG_MODE: Literal["ndjson", "per_invocation", "both"] = "ndjson"
    TOOLS_LOG_DIR: str = "/var/lib/gateway/data/tools"

    # Tool execution hard limits
    TOOLS_MAX_CONCURRENT: int = 8
    TOOLS_CONCURRENCY_TIMEOUT_SEC: float = 5.0
    TOOLS_SUBPROCESS_STDOUT_MAX_CHARS: int = 20000
    TOOLS_SUBPROCESS_STDERR_MAX_CHARS: int = 20000

    # Optional: registry integrity check (sha256 hex). If set and mismatched, registry is ignored.
    TOOLS_REGISTRY_SHA256: str = ""

    # Optional: per-bearer-token rate limit for /v1/tools endpoints.
    # Disabled when <= 0.
    TOOLS_RATE_LIMIT_RPS: float = 0.0
    TOOLS_RATE_LIMIT_BURST: int = 0

    # Optional: metrics endpoint
    METRICS_ENABLED: bool = True

    # Optional infra-owned tool registry (explicit tool declarations).
    # When present, tools can be declared with version + JSON schema + subprocess exec spec.
    TOOLS_REGISTRY_PATH: str = "/var/lib/gateway/app/tools_registry.json"

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

    # Minimal request instrumentation (JSONL). Intended for debugging/observability.
    REQUEST_LOG_ENABLED: bool = True
    REQUEST_LOG_PATH: str = "/var/lib/gateway/data/requests.jsonl"

    # Agent runtime v1 (single-process, deterministic)
    AGENT_SPECS_PATH: str = "/var/lib/gateway/app/agent_specs.json"
    AGENT_RUNS_LOG_PATH: str = "/var/lib/gateway/data/agent/runs.jsonl"
    AGENT_RUNS_LOG_DIR: str = "/var/lib/gateway/data/agent"
    AGENT_RUNS_LOG_MODE: Literal["ndjson", "per_run", "both"] = "per_run"

    # Admission control / load shedding
    AGENT_BACKEND_CONCURRENCY_OLLAMA: int = 4
    AGENT_BACKEND_CONCURRENCY_MLX: int = 2
    AGENT_QUEUE_MAX: int = 32
    AGENT_QUEUE_TIMEOUT_SEC: float = 2.0
    AGENT_SHED_HEAVY: bool = True


S = Settings()

logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper())
