"""Who's talking (voice/speaker.py) + its use in Echo, with a stand-in voice-print model (no real model needed)."""
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from voice.speaker import SpeakerID
from voice.stt import FakeSTT, decode_audio


class FakeSpeakerID(SpeakerID):
    """Voice print = energy in 8 frequency bands, so a 150 Hz tone and a 300 Hz tone are 'different people'."""
    def embed(self, audio):
        mag = np.abs(np.fft.rfft(audio[:16000]))[:800]
        v = np.array([b.sum() for b in np.array_split(mag, 8)], dtype=np.float32) + 1e-3
        return v / np.linalg.norm(v)


def tone(freq, seconds=4.0, rate=16000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def wav(freq, seconds=4.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((tone(freq, seconds, rate) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def sid(tmp_path):
    model = tmp_path / "fake.onnx"; model.write_bytes(b"x")
    return FakeSpeakerID(model, tmp_path / "speakers", threshold=0.72, margin=0.1)


def test_identify_known_unknown_and_short(sid):
    assert sid.identify(tone(150))["speaker"] == "unknown"          # nobody enrolled yet
    sid.enroll("owner", [tone(150)], ["a"])
    sid.enroll("loved_one", [tone(300)], ["b"])
    assert sid.identify(tone(150))["speaker"] == "owner"
    assert sid.identify(tone(300))["speaker"] == "loved_one"
    assert sid.identify(tone(150, seconds=0.5))["speaker"] == "unknown"   # too short to tell
    assert sid.identify(tone(700))["speaker"] == "unknown"                # nobody we know


def test_too_close_to_call_is_unknown(sid):
    sid.enroll("owner", [tone(150)], ["a"])
    sid.enroll("twin", [tone(150)], ["b"])
    assert sid.identify(tone(150))["speaker"] == "unknown"


def test_profiles_persist_add_up_and_forget(sid, tmp_path):
    sid.enroll("loved_one", [tone(300)], ["one"])
    assert sid.enroll("loved_one", [tone(300)], ["two"])["samples"] == 2
    again = FakeSpeakerID(sid.model, tmp_path / "speakers")
    assert "loved_one" in again.profiles and again.info("loved_one")["samples"] == 2
    assert again.forget("loved_one") and "loved_one" not in again.profiles and not again.forget("loved_one")
    with pytest.raises(ValueError):
        sid.enroll("../evil", [tone(300)], ["x"])
    with pytest.raises(ValueError):
        sid.enroll("loved_one", [tone(300, seconds=0.4)], ["short"])


def make(tmp_path, sid, stt_text="hello there"):
    cfg = PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), dataset_root=str(tmp_path / "ds"))
    (tmp_path / "ds").mkdir(exist_ok=True)
    return TestClient(create_app(cfg, EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(stt_text), speakers=sid))


def test_enroll_needs_consent_keeps_original_read_only_and_forget_deletes(tmp_path, sid):
    c = make(tmp_path, sid)
    assert c.post("/api/speakers/loved_one/sample", content=wav(300)).status_code == 403
    assert c.post("/api/speakers/loved_one/sample?consent=true", content=wav(300, seconds=1)).status_code == 400   # too short
    assert c.post("/api/speakers/Lo%20ri/sample?consent=true", content=wav(300)).status_code == 400
    r = c.post("/api/speakers/loved_one/sample?consent=true", content=wav(300))
    assert r.status_code == 200 and r.json()["samples"] == 1
    kept = list((tmp_path / "ds" / "raw" / "speakers" / "loved_one").glob("*.wav"))
    assert len(kept) == 1 and not (kept[0].stat().st_mode & 0o222)
    assert [p["name"] for p in c.get("/api/speakers").json()["people"]] == ["loved_one"]
    assert c.post("/api/speakers/test", content=wav(300)).json()["speaker"] == "loved_one"
    assert c.delete("/api/speakers/loved_one").status_code == 200
    assert not (tmp_path / "ds" / "raw" / "speakers" / "loved_one").exists() and c.get("/api/speakers").json()["people"] == []
    assert c.delete("/api/speakers/loved_one").status_code == 404


def test_stt_returns_speaker_only_with_kept_text(tmp_path, sid):
    sid.enroll("owner", [tone(150)], ["a"]); sid.enroll("loved_one", [tone(300)], ["b"])
    c = make(tmp_path, sid)
    assert c.post("/api/stt", content=wav(300)).json() == {"text": "hello there", "speaker": "loved_one", "speaker_score": 1.0}
    assert c.post("/api/stt?mode=wake", content=wav(300)).json() == {"wake": False}     # not for Echo: nothing
    w = make(tmp_path, sid, "hey echo what time is it").post("/api/stt?mode=wake", content=wav(150)).json()
    assert w["wake"] and w["speaker"] == "owner"


def test_stt_without_profiles_has_no_speaker(tmp_path, sid):
    assert make(tmp_path, sid).post("/api/stt", content=wav(300)).json() == {"text": "hello there"}


class Capture(EchoBackend):
    def __init__(self):
        self.seen = []

    async def chat_stream(self, messages):
        self.seen.append(messages)
        async for p in super().chat_stream(messages):
            yield p


def test_chat_gets_voice_note_and_loved_one_memories_are_marked(tmp_path, sid):
    sid.enroll("loved_one", [tone(300)], ["b"])
    b = Capture()
    cfg = PersonaConfig(memory_db=str(tmp_path / "m.sqlite"))
    store = MemoryStore(tmp_path / "m.sqlite")
    with TestClient(create_app(cfg, b, store, FakeSTT(), speakers=sid)).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Remember that my school starts at 8.", "speaker": "loved_one"})
        saved = ws.receive_json()
        assert saved["memory"]["told_by"] == "loved_one" and saved["memory"]["source"] == "loved_one:chat"
        while ws.receive_json().get("state") != "idle":
            pass
        ws.send_json({"text": "What's for dinner?", "speaker": "stranger"})   # not a known profile → ignored
        while ws.receive_json().get("state") != "idle":
            pass
        ws.send_json({"text": "Typed message"})
        while ws.receive_json().get("state") != "idle":
            pass
    users = [m[-1]["content"] for m in b.seen]
    assert users[0].startswith("[Recognised by voice: my loved one is speaking]")
    assert not users[1].startswith("[") and not users[2].startswith("[")
    assert "told by loved one" in b.seen[1][0]["content"]                       # her memory, marked as hers
    assert all(t["content"].startswith(("Remember", "What", "Typed")) or t["role"] == "assistant"
               for t in store.recent_turns(20))                           # stored conversation = what was said


# ---- room-mic prints (2026-10-31: the owner was "unknown" through the room mic and called the loved one) ----
def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_room_prints_make_a_second_centroid(tmp_path):
    import json as _json
    from voice import speaker as S
    rng = np.random.default_rng(1)
    studio, room, loved_one = _unit(rng.normal(size=16)), _unit(rng.normal(size=16)), _unit(rng.normal(size=16))
    d = tmp_path / "speakers"; d.mkdir()
    np.savez(d / "owner.npz", prints=np.stack([_unit(studio + 0.05 * rng.normal(size=16)) for _ in range(5)]),
             sources=_json.dumps(["take"] * 5), updated=0)
    np.savez(d / "loved_one.npz", prints=np.stack([loved_one]), sources=_json.dumps(["loved_one"]), updated=0)
    sid = S.SpeakerID(tmp_path / "no-model.onnx", d)
    assert sid._scores(room)["owner"] < 0.6                         # room voice: far from the studio prints
    for i in range(S.LIVE_MIN):
        sid.add_live("owner", _unit(room + 0.05 * rng.normal(size=16)), "confirmed")
    assert sid.profiles["owner"].shape[0] == 2 and sid._scores(room)["owner"] > 0.95
    assert sid._scores(studio)["owner"] > 0.95 and sid._scores(loved_one)["loved_one"] > 0.99
    for i in range(S.LIVE_MAX + 5):                                 # only the newest LIVE_MAX room prints are kept
        sid.add_live("owner", room, "auto")
    srcs = _json.loads(str(np.load(d / "owner.npz")["sources"]))
    assert sum(s.startswith("live:") for s in srcs) == S.LIVE_MAX and srcs.count("take") == 5
    with pytest.raises(ValueError):
        sid.add_live("nobody", room, "auto")
