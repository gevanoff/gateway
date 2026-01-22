from pathlib import Path
import tempfile
import yaml

from app import backends
from app.config import S


def test_load_backends_config_substitutes_settings(tmp_path, monkeypatch):
    cfg = {
        "backends": {
            "heartmula_music": {
                "class": "heartmula_music",
                "base_url": "${HEARTMULA_BASE_URL}",
                "description": "test",
                "supported_capabilities": ["music"],
                "concurrency_limits": {"music": 1},
                "health": {"liveness": "/healthz", "readiness": "/readyz"},
            }
        }
    }
    p = tmp_path / "backends.yaml"
    p.write_text(yaml.safe_dump(cfg))

    # Ensure S has the value but os.environ might not
    monkeypatch.setattr(S, "HEARTMULA_BASE_URL", "http://ada2:9920")

    reg = backends.load_backends_config(path=p)
    be = reg.get_backend("heartmula_music")
    assert be is not None
    assert be.base_url == "http://ada2:9920"
