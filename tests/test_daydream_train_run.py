"""Daydream training step 6 (scripts/daydream_train.py, core/lora_gguf.py): the overnight runner refuses unless every
condition holds, and MLX adapter names map onto Echo's GGUF tensor names. The heavy path (MLX training, merge, Ollama
import) needs the training environment and was run for real on 2026-11-04 (see CHANGELOG)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import daydream_train as R  # noqa: E402
from core import daydream_train as T, lora_gguf  # noqa: E402


def test_gguf_names():
    assert lora_gguf.gguf_name("language_model.model.layers.24.mlp.down_proj.lora_a") == "blk.24.ffn_down.weight.lora_a"
    assert lora_gguf.gguf_name("language_model.model.layers.3.mlp.gate_proj.lora_b") == "blk.3.ffn_gate.weight.lora_b"
    with pytest.raises(ValueError):
        lora_gguf.gguf_name("language_model.model.layers.3.linear_attn.in_proj_qkv.lora_a")


def _cfg(monkeypatch, **training):
    t = {"enabled": True, "min_examples": 50, "keep_versions": 2} | training
    monkeypatch.setattr(R.daydream, "load_config", lambda p: {"training": t})


def _log(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(T, "log", lambda e: seen.append(e))
    return seen


def test_off_by_default(monkeypatch, tmp_path):
    seen = _log(monkeypatch, tmp_path)
    _cfg(monkeypatch, enabled=False)
    monkeypatch.setattr(sys, "argv", ["x", "--now"])
    assert R.main() == 0 and "training is off" in seen[-1]["why"]


def test_night_window(monkeypatch, tmp_path):
    seen = _log(monkeypatch, tmp_path)
    _cfg(monkeypatch)

    class Noon(R.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 4, 12, 0)
    monkeypatch.setattr(R, "datetime", Noon)
    monkeypatch.setattr(sys, "argv", ["x"])
    assert R.main() == 0 and "night window" in seen[-1]["why"]


def test_not_while_someone_talks(monkeypatch, tmp_path):
    seen = _log(monkeypatch, tmp_path)
    _cfg(monkeypatch)
    monkeypatch.setattr(R, "last_talk_minutes", lambda: 3.0)
    monkeypatch.setattr(sys, "argv", ["x", "--now"])
    assert R.main() == 0 and "talked to Echo" in seen[-1]["why"]


def test_needs_enough_examples(monkeypatch, tmp_path):
    seen = _log(monkeypatch, tmp_path)
    _cfg(monkeypatch)
    monkeypatch.setattr(R, "last_talk_minutes", lambda: None)
    monkeypatch.setattr(sys, "argv", ["x", "--now"])
    assert R.main() == 0 and "0 kept examples, need 50" in seen[-1]["why"]


def test_modelfile_keeps_echo_settings(monkeypatch):
    fake = ("# comment\nFROM /blobs/weights\nFROM /blobs/vision\nTEMPLATE {{ .Prompt }}\nRENDERER qwen3.5\n"
            "PARSER qwen3.5\nPARAMETER top_k 20\n")
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: type("r", (), {"stdout": fake})())
    out = R.modelfile_for("qwen3.5:9b-q4_K_M", Path("/w/merged.gguf"))
    assert out.splitlines()[:3] == ["FROM /w/merged.gguf", "FROM /blobs/vision", "TEMPLATE {{ .Prompt }}"]
    assert "RENDERER qwen3.5" in out and "# comment" not in out
    assert R.base_gguf("x") == Path("/blobs/weights")
