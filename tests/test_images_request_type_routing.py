import os


os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")


from app.images_backend import _select_images_model


def test_images_auto_photoreal_selects_slow(monkeypatch):
    import app.images_backend as ib

    monkeypatch.setattr(ib.S, "IMAGES_ENABLE_REQUEST_TYPE", True)
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL", "auto")
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL_FAST", "gpu_fast")
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL_SLOW", "gpu_slow")

    m, reason = _select_images_model(prompt="photorealistic DSLR portrait photo, skin texture", requested_model="auto")
    assert m == "gpu_slow"
    assert reason == "policy:images->slow"


def test_images_auto_default_selects_fast(monkeypatch):
    import app.images_backend as ib

    monkeypatch.setattr(ib.S, "IMAGES_ENABLE_REQUEST_TYPE", True)
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL", "auto")
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL_FAST", "gpu_fast")
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL_SLOW", "gpu_slow")

    m, reason = _select_images_model(prompt="a cute cartoon cat", requested_model="auto")
    assert m == "gpu_fast"
    assert reason == "policy:images->fast"


def test_images_explicit_model_wins(monkeypatch):
    import app.images_backend as ib

    monkeypatch.setattr(ib.S, "IMAGES_ENABLE_REQUEST_TYPE", True)
    monkeypatch.setattr(ib.S, "IMAGES_OPENAI_MODEL", "auto")

    m, reason = _select_images_model(prompt="photorealistic", requested_model="some-model")
    assert m == "some-model"
    assert reason == "request:model"
