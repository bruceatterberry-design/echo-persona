import hashlib
import json
import subprocess
import sys

import pytest

import processing.ingest as ing
from tests.test_face import FAKE
from tests.test_studio import voice_like
from voice.stt import FakeSTT


@pytest.fixture
def ds(tmp_path, monkeypatch):
    fake = tmp_path / "facefind"
    fake.write_text(FAKE.format(py=sys.executable)); fake.chmod(0o755)
    monkeypatch.setattr(ing, "FACEFIND", fake)
    root = tmp_path / "ds"
    s = root / "raw/phone/20261101-090000"; s.mkdir(parents=True)
    wav = voice_like(3); (s / "take-001.wav").write_bytes(wav)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=10:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=200:duration=2", "-shortest", str(s / "take-002.mp4")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1600x1200", "-frames:v", "1",
                    str(s / "take-003.jpg")], check=True)
    (s / "take-004.wav").write_bytes(wav)
    takes = [
        {"file": "take-001.wav", "take": "take-001", "task_id": "calm-01", "task_kind": "voice", "status": "kept",
         "prompt_text": "It's all right. Take a deep breath. We'll figure this out together."},
        {"file": "take-002.mp4", "take": "take-002", "task_id": "about_me-03", "task_kind": "answer", "status": "kept",
         "prompt_text": "How do you show respect?", "expression_label": None},
        {"file": "take-003.jpg", "take": "take-003", "task_id": "photo-straight", "task_kind": "photo", "status": "kept"},
        {"file": "take-004.wav", "take": "take-004", "task_id": "calm-02", "task_kind": "voice", "status": "rejected"},
    ]
    for t in takes:
        t["sha256"] = hashlib.sha256((s / t["file"]).read_bytes()).hexdigest()
    (s / "manifest.json").write_text(json.dumps({"takes": takes}))
    (s / "receipt.json").write_text("{}")
    return root


def test_ingest_routes_each_kind_once(ds):
    stt = FakeSTT("It's all right. Take a deep breath. We'll figure this out together.")
    assert ing.pending(ds) == ["20261101-090000"]
    rep = ing.process_all(ds, stt)[0]
    assert (rep["voice"], rep["videos"], rep["photos"], rep["answers"]) == (1, 1, 1, 1) and not rep["errors"]
    qc = json.loads((ds / "derived/voice/qc-v1/phone-20261101-090000/take-001.json").read_text())
    assert qc["read_match"] == 1.0 and qc["status"] == "kept" and qc["source"].startswith("raw/phone/")
    assert not (ds / "derived/voice/qc-v1/phone-20261101-090000/take-004.json").exists()   # rejected takes skipped
    frames = json.loads((ds / "derived/face/phone-frames-v1/20261101-090000/take-002/frames.json").read_text())
    assert frames["frames"] >= 3 and frames["kept"]
    ex = [json.loads(l) for l in (ds / "derived/personality/examples-v1.jsonl").read_text().splitlines()]
    assert ex[0]["question"] == "How do you show respect?" and ex[0]["reviewed"] is False
    assert (ds / "staging/face/photos-current/phone/20261101-090000-take-003.jpg").exists()
    assert ing.pending(ds) == [] and ing.process_all(ds, stt) == []        # done once


def test_studio_stats_count_phone_voice(ds):
    from voice.studio import Studio
    ing.process_all(ds, FakeSTT("x"))
    st = Studio(ds).stats()
    assert st["takes"] == 1 and "calm-01" in st["kept_prompt_ids"]
