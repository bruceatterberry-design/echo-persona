import importlib.util
import json

from core.config import ROOT

spec = importlib.util.spec_from_file_location("make_voice_reference", ROOT / "scripts" / "make_voice_reference.py")
mvr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mvr)


def take(ds, session, name, prompt, **qc):
    (ds / "raw/voice" / session).mkdir(parents=True, exist_ok=True)
    (ds / "raw/voice" / session / f"{name}.wav").write_bytes(b"RIFF" + name.encode())
    q = ds / "derived/voice/qc-v1" / session
    q.mkdir(parents=True, exist_ok=True)
    base = {"take": name, "session": session, "prompt_id": prompt, "status": "kept", "verdict": "good",
            "duration_s": 7.0, "read_match": 1.0, "snr_db": 30, "transcript": f"words of {name}"}
    (q / f"{name}.json").write_text(json.dumps(base | qc))


def test_picks_clean_kept_takes_and_sets_active(tmp_path):
    ds, cfg = tmp_path / "ds", tmp_path / "config"
    cfg.mkdir()
    (cfg / "voice.json").write_text(json.dumps({"stt": "parakeet"}))
    take(ds, "s1", "take-0001", "storytelling-01", snr_db=35)
    take(ds, "s1", "take-0002", "storytelling-02", snr_db=42)                    # best: picked
    take(ds, "s1", "take-0003", "storytelling-03", snr_db=60, status="rejected")  # not kept
    take(ds, "s1", "take-0004", "storytelling-04", snr_db=60, duration_s=3.0)     # too short
    take(ds, "s1", "take-0005", "factual-01", snr_db=50, read_match=0.6)          # misread
    assert mvr.main("storytelling", ds, cfg) == 0
    refs = json.loads((ds / "derived/voice/refs-v1/refs.json").read_text())
    assert list(refs) == ["storytelling"] and refs["storytelling"]["text"] == "words of take-0002"
    assert (ds / "derived/voice/refs-v1/ref-storytelling.wav").read_bytes() == b"RIFFtake-0002"
    assert json.loads((cfg / "voice.json").read_text()) == {"stt": "parakeet", "reference": "storytelling"}


def test_nothing_recorded_yet(tmp_path):
    assert mvr.main("storytelling", tmp_path / "ds", tmp_path) == 1
