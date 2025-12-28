from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Literal, Optional, Tuple

from app.model_aliases import get_alias, get_aliases

Backend = Literal["ollama", "mlx"]


@dataclass(frozen=True)
class RouteDecision:
    backend: Backend
    model: str
    reason: str


@dataclass(frozen=True)
class RouterConfig:
    default_backend: Backend

    # Model choices per backend
    ollama_strong_model: str
    ollama_fast_model: str
    mlx_strong_model: str
    mlx_fast_model: str

    # Heuristic thresholds
    long_context_chars_threshold: int = 40_000


def _approx_text_size(messages: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = (m or {}).get("content")
        if isinstance(c, str):
            n += len(c)
        elif c is None:
            continue
        else:
            try:
                n += len(json.dumps(c))
            except Exception:
                n += 0
    return n


def _choose_backend_by_model(model: str, default_backend: Backend) -> Backend:
    m = (model or "").strip().lower()

    if m.startswith("ollama:"):
        return "ollama"
    if m.startswith("mlx:"):
        return "mlx"

    if m in {"ollama", "ollama-default"}:
        return "ollama"
    if m in {"mlx", "mlx-default"}:
        return "mlx"

    return default_backend


def _normalize_model(model: str, backend: Backend, cfg: RouterConfig) -> str:
    m = (model or "").strip()

    if backend == "ollama":
        if m.startswith("ollama:"):
            m = m[len("ollama:") :]
        if m in {"default", "ollama", ""}:
            return cfg.ollama_strong_model
        return m

    if m.startswith("mlx:"):
        m = m[len("mlx:") :]
    if m in {"default", "mlx", ""}:
        return cfg.mlx_strong_model
    return m


def decide_route(
    *,
    cfg: RouterConfig,
    request_model: str,
    headers: Dict[str, str],
    messages: Optional[Iterable[Dict[str, Any]]] = None,
    has_tools: bool = False,
) -> RouteDecision:
    """Select {backend, model} with simple, stable heuristics.

    Overrides:
    - header x-backend: ollama|mlx
    - model prefix: ollama:... or mlx:...
    - explicit model name: passes through

    Policy:
    - tool-heavy/agentic => strong model
    - long context => prefer mlx strong (if configured) else default strong
    - otherwise => fast/cheap model on chosen backend
    """

    hdr_backend = (headers.get("x-backend") or "").strip().lower()
    if hdr_backend in {"ollama", "mlx"}:
        backend: Backend = hdr_backend  # type: ignore[assignment]
        normalized = _normalize_model(request_model, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="override:x-backend")

    aliases = get_aliases()

    # Model aliases: if request_model is an alias key (coder/fast/default/long/etc),
    # resolve directly to a stable backend + upstream model.
    alias_key = (request_model or "").strip().lower()
    if alias_key and alias_key in aliases:
        a = aliases[alias_key]
        backend = a.backend  # type: ignore[assignment]
        normalized = _normalize_model(a.upstream_model, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="alias:model")

    backend = _choose_backend_by_model(request_model, cfg.default_backend)

    explicitly_pinned = (request_model or "").strip().lower().startswith(("ollama:", "mlx:")) or (request_model or "").strip().lower() in {
        "ollama",
        "mlx",
        "ollama-default",
        "mlx-default",
    }

    # If explicitly pinned, honor it and only normalize aliases/defaults.
    if explicitly_pinned:
        normalized = _normalize_model(request_model, backend, cfg)
        return RouteDecision(backend=backend, model=normalized, reason="pinned:model")

    size = _approx_text_size(messages or [])

    # If aliases declare a context window, prefer it for thresholding.
    long_alias = get_alias("long")
    long_threshold = int(long_alias.context_window) if (long_alias and long_alias.context_window) else cfg.long_context_chars_threshold

    if has_tools:
        # Deterministic rule: tools -> strongest tool-capable model.
        a = get_alias("default")
        if a and a.tools is not False:
            b = a.backend  # type: ignore[assignment]
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:tools->alias:default")
        # If default explicitly doesn't support tools, prefer coder if it does.
        a = get_alias("coder")
        if a and a.tools is not False:
            b = a.backend  # type: ignore[assignment]
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:tools->alias:coder")
        if backend == "ollama":
            return RouteDecision(backend=backend, model=cfg.ollama_strong_model, reason="policy:tools->strong")
        return RouteDecision(backend=backend, model=cfg.mlx_strong_model, reason="policy:tools->strong")

    if size >= long_threshold:
        # Prefer MLX for long-context if available, otherwise keep backend but use strong model.
        a = get_alias("long")
        if a:
            b = a.backend  # type: ignore[assignment]
            return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:long_context->alias:long")
        if cfg.mlx_strong_model:
            return RouteDecision(backend="mlx", model=cfg.mlx_strong_model, reason="policy:long_context->mlx")
        if backend == "ollama":
            return RouteDecision(backend=backend, model=cfg.ollama_strong_model, reason="policy:long_context->strong")
        return RouteDecision(backend=backend, model=cfg.mlx_strong_model, reason="policy:long_context->strong")

    # Default: fast/cheap on chosen backend
    a = get_alias("fast")
    if a:
        b = a.backend  # type: ignore[assignment]
        return RouteDecision(backend=b, model=_normalize_model(a.upstream_model, b, cfg), reason="policy:fast->alias:fast")

    if backend == "ollama":
        return RouteDecision(backend=backend, model=cfg.ollama_fast_model, reason="policy:fast")
    return RouteDecision(backend=backend, model=cfg.mlx_fast_model, reason="policy:fast")
