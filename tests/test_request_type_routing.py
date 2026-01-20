import os


# Ensure config is set before importing app modules
os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")


from app.router import RouterConfig, decide_route
from app.model_aliases import ModelAlias
import app.router as router_mod


def test_auto_model_coding_prefers_coder_alias(monkeypatch):
    cfg = RouterConfig(
        default_backend="ollama",
        ollama_strong_model="strong",
        ollama_fast_model="fast",
        mlx_strong_model="mlx-strong",
        mlx_fast_model="mlx-fast",
        long_context_chars_threshold=40_000,
    )

    def fake_get_alias(name: str):
        if name == "coder":
            return ModelAlias(backend="ollama", upstream_model="deepseek-coder:33b", tools=True)
        return None

    monkeypatch.setattr(router_mod, "get_alias", fake_get_alias)

    r = decide_route(
        cfg=cfg,
        request_model="auto",
        headers={},
        messages=[{"role": "user", "content": "Please fix this Python traceback:\n```\nTraceback ...\n```"}],
        has_tools=False,
        enable_policy=True,
        enable_request_type=True,
    )
    assert r.backend == "ollama"
    assert r.model == "deepseek-coder:33b"
    assert r.reason == "policy:coding->alias:coder"


def test_auto_model_non_coding_defaults_to_fast(monkeypatch):
    cfg = RouterConfig(
        default_backend="ollama",
        ollama_strong_model="strong",
        ollama_fast_model="fast",
        mlx_strong_model="mlx-strong",
        mlx_fast_model="mlx-fast",
        long_context_chars_threshold=40_000,
    )

    # Avoid unexpected alias routing; only need coder lookup to return None.
    monkeypatch.setattr(router_mod, "get_alias", lambda _name: None)

    r = decide_route(
        cfg=cfg,
        request_model="auto",
        headers={},
        messages=[{"role": "user", "content": "What's a good weekend recipe?"}],
        has_tools=False,
        enable_policy=True,
        enable_request_type=True,
    )
    assert r.backend == "ollama"
    assert r.model == "fast"
    assert r.reason in {"policy:fast", "policy:fast->alias:fast"}


def test_x_request_type_header_forces_coding(monkeypatch):
    cfg = RouterConfig(
        default_backend="ollama",
        ollama_strong_model="strong",
        ollama_fast_model="fast",
        mlx_strong_model="mlx-strong",
        mlx_fast_model="mlx-fast",
        long_context_chars_threshold=40_000,
    )

    def fake_get_alias(name: str):
        if name == "coder":
            return ModelAlias(backend="ollama", upstream_model="coder-model", tools=True)
        return None

    monkeypatch.setattr(router_mod, "get_alias", fake_get_alias)

    r = decide_route(
        cfg=cfg,
        request_model="auto",
        headers={"x-request-type": "coding"},
        messages=[{"role": "user", "content": "hi"}],
        has_tools=False,
        enable_policy=True,
        enable_request_type=True,
    )
    assert r.backend == "ollama"
    assert r.model == "coder-model"
    assert r.reason == "policy:coding->alias:coder"
