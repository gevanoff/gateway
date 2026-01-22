import json
from pathlib import Path

import pytest

from app import agent_runtime_v1 as agent_rt
from app.config import S


def test_load_agent_specs_from_file(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "env" / "agent_specs.json.example"
    assert example.exists()
    data = json.loads(example.read_text(encoding="utf-8"))

    p = tmp_path / "agent_specs.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(S, "AGENT_SPECS_PATH", str(p))

    specs = agent_rt.load_agent_specs()
    assert isinstance(specs, dict)
    assert "music" in specs
    music = specs.get("music")
    assert music is not None
    assert int(music.tier) == 1
    assert isinstance(music.tools_allowlist, list)
    assert "heartmula_generate" in music.tools_allowlist
