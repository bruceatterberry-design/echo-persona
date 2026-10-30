"""Suggested life memories (Phase 5a): grounding, review, ingest hook, API."""
import json

import numpy as np
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory import candidates as mc
from memory.store import Memory, MemoryStore
from processing import ingest

TRANSCRIPT = ("Well when I was a kid in Springfield we lived on West Street. In the summer of 1984 my dad and my brother "
              "Tom took me fishing at the creek and I caught a big catfish. We fried it up that night.")


def test_grounding_drops_invented_quotes_and_unsaid_years():
    items = [
        {"memory": "Dad and Tom took me fishing at the creek.", "when": "summer of 1984", "year": 1984, "people": "Dad, Tom",
         "place": "the creek", "period": "childhood", "quote": "my dad and my brother Tom took me fishing at the creek"},
        {"memory": "I won a trophy.", "when": "", "year": 0, "people": "", "place": "", "period": "teen",
         "quote": "I won the state trophy that year"},                                    # not in the transcript
        {"memory": "We fried the fish.", "when": "", "year": 1985, "people": "", "place": "", "period": "unknown",
         "quote": "We fried it up that night"},                                           # 1985 never said
        {"memory": "Short.", "when": "", "year": 0, "people": "", "place": "", "period": "x", "quote": "a kid"},  # too short
    ]
    got = mc.grounded(items, TRANSCRIPT, default_period="childhood")
    assert [g["content"] for g in got] == ["Dad and Tom took me fishing at the creek.", "We fried the fish."]
    assert got[0]["year"] == 1984 and got[1]["year"] is None and got[1]["period"] == "childhood"


def test_candidate_review_flow(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    s.save(Memory(content="I have two dogs, Max and Ruby."))
    assert s.add_candidate({"content": "I have two dogs, Max and Ruby", "quote": "q", "source": "raw/a.wav"}) is None
    a = s.add_candidate({"content": "Dad took me fishing at the creek.", "quote": "took me fishing", "source": "raw/a.wav",
                         "period": "childhood"})
    assert s.add_candidate({"content": "Dad took me fishing at the creek!", "quote": "x", "source": "raw/b.wav"}) is None
    b = s.add_candidate({"content": "We fried the fish that night.", "quote": "fried it", "source": "raw/a.wav"})
    m, dup = s.approve_candidate(a, {"content": "Dad and Tom took me fishing at the creek.", "year": 1984, "quote": "hack"})
    assert not dup and (m.kind, m.status, m.told_by, m.year, m.period) == ("memory", "confirmed", "owner", 1984, "childhood")
    assert m.source == "owner:recording:raw/a.wav" and s.candidate(a)["quote"] == "took me fishing"   # evidence unchanged
    assert s.approve_candidate(a) is None                                                        # once only
    assert s.reject_candidate(b) and not s.reject_candidate(b)
    assert s.add_candidate({"content": "We fried the fish that night.", "quote": "x", "source": "raw/a.wav"}) is None  # rejected stays out
    assert s.candidates() == []


def test_transcribe_long_cuts_in_pieces():
    class Count:
        calls = []
        def transcribe_sync(self, a):
            self.calls.append(len(a)); return "words"
    sr = 16000
    audio = np.random.default_rng(0).normal(0, 0.1, 150 * sr).astype(np.float32)
    audio[55 * sr:55 * sr + 1600] = 0                       # a quiet moment to cut at
    c = Count()
    assert ingest.transcribe_long(c, audio) == "words words words"
    assert sum(c.calls) == len(audio) and all(n <= 60 * sr for n in c.calls) and abs(c.calls[0] - 55.05 * sr) < 0.2 * sr


def test_ingest_memory_take_saves_transcript_and_calls_back(tmp_path, monkeypatch):
    import io, wave, hashlib
    root = tmp_path / "ds"; sess = root / "raw" / "phone" / "s1"; sess.mkdir(parents=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.sin(np.arange(32000) / 5) * 8000).astype(np.int16).tobytes())
    (sess / "take-001.wav").write_bytes(buf.getvalue())
    (sess / "manifest.json").write_text(json.dumps({"takes": [{"take": "take-001", "file": "take-001.wav", "task_id": "mem-child-01",
        "task_kind": "memory", "status": "kept", "prompt_text": "Earliest memory?", "life_period": "childhood",
        "sha256": hashlib.sha256(buf.getvalue()).hexdigest()}]}))
    (sess / "receipt.json").write_text("{}")

    class STT:
        def transcribe_sync(self, a): return TRANSCRIPT
    told = []
    rep = ingest.process_session(root, "s1", STT(), on_memory=told.append)
    assert rep["memories"] == 1 and told[0]["period"] == "childhood" and told[0]["transcript"] == TRANSCRIPT
    assert json.loads((root / "derived/memories/transcripts-v1/s1/take-001.json").read_text())["question"] == "Earliest memory?"
    qc = json.loads((root / "derived/voice/qc-v1/phone-s1/take-001.json").read_text())
    assert "read_match" not in qc


def test_candidate_api_and_audio_stays_inside_dataset(tmp_path):
    root = tmp_path / "ds"; (root / "raw" / "phone").mkdir(parents=True)
    (root / "raw" / "phone" / "a.wav").write_bytes(b"RIFF....")
    (tmp_path / "secret.txt").write_text("no")
    store = MemoryStore(tmp_path / "m.sqlite")
    cfg = PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), dataset_root=str(root))
    c = TestClient(create_app(cfg, EchoBackend(), store))
    a = store.add_candidate({"content": "Dad took me fishing.", "quote": "took me fishing", "source": "raw/phone/a.wav",
                             "question": "Q", "period": "childhood"})
    bad = store.add_candidate({"content": "Sneaky path.", "quote": "x x x x", "source": "../secret.txt"})
    got = c.get("/api/memories/candidates").json()
    assert {g["id"] for g in got} == {a, bad} and got[0]["similar"] is None
    assert c.get(f"/api/memories/candidates/{a}/audio").status_code == 200
    assert c.get(f"/api/memories/candidates/{bad}/audio").status_code == 404
    r = c.post(f"/api/memories/candidates/{a}", json={"action": "approve", "people": "Dad", "year": 1984}).json()
    assert r["kind"] == "memory" and r["people"] == "Dad" and r["duplicate"] is False
    assert c.post(f"/api/memories/candidates/{a}", json={"action": "approve"}).status_code == 404
    assert c.post(f"/api/memories/candidates/{bad}", json={"action": "approve", "year": 1200}).status_code == 422
    assert c.post(f"/api/memories/candidates/{bad}", json={"action": "reject"}).status_code == 200
    life = c.get("/api/memories/life").json()
    assert [m["content"] for m in life] == ["Dad took me fishing."]
