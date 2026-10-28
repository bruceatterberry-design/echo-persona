import hashlib
import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from voice.stt import FakeSTT
from voice.studio import Studio, StudioError, load_prompts, quality, read_match, read_wav
from tests.test_voice import synthetic_wav


def voice_like(seconds=3.0, rate=48000, amp=0.3, noise=0.002, clip=False):
    """Bursts of tone (syllables) over a quiet noise floor."""
    import io, wave
    t = np.arange(int(seconds * rate)) / rate
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
    x = amp * env * np.sin(2 * np.pi * 180 * t) + np.random.default_rng(1).normal(0, noise, len(t))
    if clip:
        x = np.clip(x * 5, -1, 1)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return b.getvalue()


@pytest.fixture
def studio(tmp_path):
    s = Studio(tmp_path / "ds")
    (tmp_path / "ds").mkdir()
    return s


def test_prompts_load_with_stable_ids():
    p = load_prompts()
    assert len(p) >= 80 and "calm-01" in p and p["about_me-01"]["read"] is False
    assert not any("echo" in x["text"].lower() and "hey" in x["text"].lower() for x in p.values())


def test_quality_good_quiet_clipped():
    a, r = read_wav(voice_like())
    assert quality(a, r)["verdict"] == "good"
    a, r = read_wav(voice_like(amp=0.003))
    assert "Too quiet" in " ".join(quality(a, r)["issues"])
    a, r = read_wav(voice_like(clip=True))
    assert quality(a, r)["verdict"] == "fail" and "clipping" in " ".join(quality(a, r)["issues"])
    a, r = read_wav(voice_like(noise=0.08))
    assert "noise" in " ".join(quality(a, r)["issues"])


def test_read_match():
    assert read_match("Good morning.", "good morning") == 1.0
    assert read_match("The moon reflects light.", "the spoon deflects night") < 0.8


def test_rejects_non_wav_and_stereo():
    with pytest.raises(StudioError):
        read_wav(b"RIFFjunk" * 20)


def test_consent_required(studio):
    sid = studio.new_session()
    with pytest.raises(StudioError, match="consent"):
        studio.save_take(sid, voice_like(), "calm-01")


def test_take_is_immutable_with_checksum(studio):
    studio.give_consent("Test Person")
    sid = studio.new_session()
    data = voice_like()
    qc = studio.save_take(sid, data, "calm-01")
    wav = studio.raw_path(sid, qc["take"])
    assert wav.read_bytes() == data and not os.access(wav, os.W_OK)
    meta = json.loads(wav.with_suffix(".meta.json").read_text())
    assert meta["sha256"] == hashlib.sha256(data).hexdigest() == qc["source_sha256"]
    assert meta["capture"]["noise_suppression"] is False
    # a second take gets a new number; nothing is overwritten
    assert studio.save_take(sid, data, "calm-02")["take"] == "take-0002"


def test_status_and_stats(studio):
    studio.give_consent("The owner")
    sid = studio.new_session()
    t1 = studio.save_take(sid, voice_like(6), "calm-01")["take"]
    t2 = studio.save_take(sid, voice_like(6), "calm-02")["take"]
    studio.set_status(sid, t1, "kept"); studio.set_status(sid, t2, "rejected")
    st = studio.stats()
    assert st["takes"] == 2 and st["kept_prompt_ids"] == ["calm-01"] and st["kept_minutes"] == 0.1
    with pytest.raises(StudioError):
        studio.set_status(sid, t1, "deleted")


@pytest.mark.parametrize("sid,take", [("../../etc", "take-0001"), ("20261027-120000", "../x"), ("20261027-120000", "take-1")])
def test_path_traversal_rejected(studio, sid, take):
    with pytest.raises(StudioError):
        studio.get_qc(sid, take)


def test_unknown_prompt_and_session(studio):
    studio.give_consent("The owner")
    with pytest.raises(StudioError):
        studio.save_take(studio.new_session(), voice_like(), "nope-99")
    with pytest.raises(StudioError):
        studio.save_take("20200101-000000", voice_like(), "calm-01")


def test_missing_drive(tmp_path):
    s = Studio(tmp_path / "not-mounted")
    assert not s.available()
    with pytest.raises(StudioError, match="plugged in"):
        s.new_session()


def test_api_full_flow(tmp_path):
    (tmp_path / "ds").mkdir()
    st = Studio(tmp_path / "ds")
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"),
                              FakeSTT("It's all right. Take a deep breath. We'll figure this out together."), st))
    assert c.get("/api/studio/status").json()["consent"] is None
    assert c.post("/api/studio/session").status_code == 200
    assert c.post("/api/studio/consent", json={"name": "Test Person"}).json()["scope"]
    sid = c.post("/api/studio/session").json()["session"]
    qc = c.post(f"/api/studio/take?session={sid}&prompt_id=calm-01", content=voice_like()).json()
    assert qc["read_match"] == 1.0 and qc["verdict"] == "good"
    assert c.patch(f"/api/studio/take/{sid}/{qc['take']}", json={"status": "kept"}).json()["status"] == "kept"
    assert c.get("/api/studio/status").json()["stats"]["kept_prompt_ids"] == ["calm-01"]
    bad = c.post(f"/api/studio/take?session={sid}&prompt_id=calm-02", content=b"not a wav")
    assert bad.status_code == 400
    assert "VOICE STUDIO" in c.get("/studio.html").text
