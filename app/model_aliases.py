from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.config import S


@dataclass(frozen=True)
class ModelAlias:
    backend: str  # "ollama" | "mlx"
    upstream_model: str
    context_window: Optional[int] = None


def _default_aliases() -> Dict[str, ModelAlias]:
    # Sensible defaults if no explicit config is provided.
    default_backend = S.DEFAULT_BACKEND

    def strong_for(backend: str) -> str:
        return S.OLLAMA_MODEL_STRONG if backend == "ollama" else S.MLX_MODEL_STRONG

    def fast_for(backend: str) -> str:
        return S.OLLAMA_MODEL_FAST if backend == "ollama" else S.MLX_MODEL_FAST

    return {
        "default": ModelAlias(backend=default_backend, upstream_model=strong_for(default_backend)),
        "fast": ModelAlias(backend=default_backend, upstream_model=fast_for(default_backend)),
        "coder": ModelAlias(backend="ollama", upstream_model=S.OLLAMA_MODEL_STRONG),
        "long": ModelAlias(backend="mlx", upstream_model=S.MLX_MODEL_STRONG, context_window=S.ROUTER_LONG_CONTEXT_CHARS),
    }


def _parse_alias_value(v: Any) -> Optional[ModelAlias]:
    # Accept either:
    # - "ollama:qwen3:30b" / "mlx:..."
    # - {"backend": "ollama", "model": "qwen3:30b", "context": 8192}
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.startswith("ollama:"):
            return ModelAlias(backend="ollama", upstream_model=s[len("ollama:") :])
        if s.startswith("mlx:"):
            return ModelAlias(backend="mlx", upstream_model=s[len("mlx:") :])
        return None

    if isinstance(v, dict):
        backend = (v.get("backend") or "").strip().lower()
        model = (v.get("model") or v.get("upstream_model") or "").strip()
        if backend not in {"ollama", "mlx"} or not model:
            return None
        if model.startswith("ollama:"):
            model = model[len("ollama:") :]
        elif model.startswith("mlx:"):
            model = model[len("mlx:") :]

        context = v.get("context") or v.get("context_window") or v.get("window")
        context_window: Optional[int] = None
        if isinstance(context, int) and context > 0:
            context_window = context
        return ModelAlias(backend=backend, upstream_model=model, context_window=context_window)

    return None


def load_aliases() -> Dict[str, ModelAlias]:
    aliases: Dict[str, ModelAlias] = dict(_default_aliases())

    raw_json = (S.MODEL_ALIASES_JSON or "").strip()
    path = (S.MODEL_ALIASES_PATH or "").strip()

    payload: Any = None
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except Exception:
            payload = None
    elif path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            payload = None

    if isinstance(payload, dict) and isinstance(payload.get("aliases"), dict):
        payload = payload["aliases"]

    if isinstance(payload, dict):
        for k, v in payload.items():
            if not isinstance(k, str):
                continue
            parsed = _parse_alias_value(v)
            if parsed:
                aliases[k.strip().lower()] = parsed

    return aliases


def resolve_alias(model: str) -> Optional[Tuple[str, str]]:
    m = (model or "").strip().lower()
    if not m:
        return None
    a = load_aliases().get(m)
    if not a:
        return None
    return a.backend, a.upstream_model
