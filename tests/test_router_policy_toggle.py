from app.router import RouterConfig, decide_route


def test_policy_disabled_is_direct_model():
    cfg = RouterConfig(
        default_backend="ollama",
        ollama_strong_model="strong",
        ollama_fast_model="fast",
        mlx_strong_model="mlx-strong",
        mlx_fast_model="mlx-fast",
        long_context_chars_threshold=10,
    )

    # Without explicit prefix or alias, and with policy disabled, the model should pass through.
    r = decide_route(cfg=cfg, request_model="qwen2.5:7b", headers={}, messages=[{"role": "user", "content": "hi"}], has_tools=False, enable_policy=False)
    assert r.backend == "ollama"
    assert r.model == "qwen2.5:7b"
    assert r.reason == "direct:model"


def test_policy_disabled_honors_pinned_prefix():
    cfg = RouterConfig(
        default_backend="ollama",
        ollama_strong_model="strong",
        ollama_fast_model="fast",
        mlx_strong_model="mlx-strong",
        mlx_fast_model="mlx-fast",
        long_context_chars_threshold=10,
    )

    r = decide_route(cfg=cfg, request_model="mlx:abc", headers={}, messages=[], has_tools=False, enable_policy=False)
    assert r.backend == "mlx"
    assert r.model == "abc"
    assert r.reason == "pinned:model"
