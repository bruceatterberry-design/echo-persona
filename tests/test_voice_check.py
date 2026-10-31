"""Echo asks "Is that you, Pat?" when an unclear voice sounds most like the owner; "yes" teaches his room print."""
import io
import wave

import numpy as np
from fastapi.testclient import TestClient

import core.server
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


class FakeSpeakers:
    available = True
    threshold = 0.72

    def __init__(self, *a, **k):
        self.profiles = {"owner": 1, "loved_one": 1}
        self.added = []
        self.result = {"speaker": "unknown", "score": 0.41, "scores": {"owner": 0.41, "loved_one": 0.25}, "best": "owner",
                       "gap": 0.16, "emb": np.ones(4, dtype=np.float32)}

    def identify(self, audio):
        return dict(self.result)

    def add_live(self, name, emb, source):
        self.added.append((name, source))
        return len(self.added)

    def info(self, n):
        return {"name": n}


class FakeSTT:
    name = "fake"

    async def transcribe(self, audio):
        return "what a lovely evening"


def wav() -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.random.default_rng(0).normal(0, 3000, 32000)).astype(np.int16).tobytes())
    return b.getvalue()


def test_unclear_owner_is_asked_and_yes_teaches(tmp_path, monkeypatch):
    fake = FakeSpeakers()
    monkeypatch.setattr(core.server, "SpeakerID", lambda *a, **k: fake)
    client = TestClient(create_app(PersonaConfig(owner_name="Pat", loved_one_name="Sam"), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt=FakeSTT()))
    heard = client.post("/api/stt?mode=direct", content=wav()).json()
    assert heard["speaker"] == "unknown"
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": heard["text"], "speaker": "unknown"})
        r = ws.receive_json()
        while r["type"] != "reply":
            r = ws.receive_json()
        assert "Is that you, Pat?" in r["text"]                 # EchoBackend repeats the note it was given
        ws.send_json({"text": "Yes, it's me", "speaker": "unknown"})
        r = ws.receive_json()
        while r["type"] != "reply":
            r = ws.receive_json()
        assert r["text"] == "Thanks, Pat. I'll know your voice better now."
    assert fake.added == [("owner", "confirmed")]


def test_confident_owner_adds_a_room_print_now_and_then(tmp_path, monkeypatch):
    fake = FakeSpeakers()
    fake.result = {**fake.result, "speaker": "owner", "score": 0.9, "gap": 0.4}
    monkeypatch.setattr(core.server, "SpeakerID", lambda *a, **k: fake)
    client = TestClient(create_app(PersonaConfig(owner_name="Pat", loved_one_name="Sam"), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt=FakeSTT()))
    for _ in range(3):
        client.post("/api/stt?mode=direct", content=wav())
    assert fake.added == [("owner", "auto")]                        # once, then not again for 6 hours


def test_loved_one_or_unclear_loved_one_never_grows_owner(tmp_path, monkeypatch):
    fake = FakeSpeakers()
    fake.result = {**fake.result, "best": "loved_one", "scores": {"owner": 0.25, "loved_one": 0.41}}
    monkeypatch.setattr(core.server, "SpeakerID", lambda *a, **k: fake)
    client = TestClient(create_app(PersonaConfig(owner_name="Pat", loved_one_name="Sam"), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt=FakeSTT()))
    client.post("/api/stt?mode=direct", content=wav())
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "what a lovely evening", "speaker": "unknown"})
        r = ws.receive_json()
        while r["type"] != "reply":
            r = ws.receive_json()
        assert "Is that you, Pat?" not in r["text"]
    assert fake.added == []


def said(client, text, result, fake):
    fake.result = {**fake.result, **result}
    client.post("/api/stt?mode=direct", content=wav())
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text, "speaker": "unknown"})
        r = ws.receive_json()
        while r["type"] != "reply":
            r = ws.receive_json()
        return r["text"]


def test_saying_your_name_gives_a_voice_sample(tmp_path, monkeypatch):
    fake = FakeSpeakers()
    monkeypatch.setattr(core.server, "SpeakerID", lambda *a, **k: fake)
    client = TestClient(create_app(PersonaConfig(owner_name="Pat", loved_one_name="Sam"), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt=FakeSTT()))
    said(client, "This is Pat.", {"scores": {"owner": 0.41, "loved_one": 0.30}, "best": "owner"}, fake)
    assert fake.added == [("owner", "said")]
    said(client, "Hi Echo, it's Sam", {"scores": {"owner": 0.30, "loved_one": 0.45}, "best": "loved_one"}, fake)
    assert fake.added[-1] == ("loved_one", "said")
    said(client, "This is Pat", {"speaker": "loved_one", "scores": {"owner": 0.40, "loved_one": 0.85}, "best": "loved_one"}, fake)
    assert len(fake.added) == 2                                                    # clearly the loved one joking: no sample
    said(client, "It's Pat's birthday", {"scores": {"owner": 0.41, "loved_one": 0.30}}, fake)
    assert len(fake.added) == 2


def test_typed_name_gives_no_sample(tmp_path, monkeypatch):
    fake = FakeSpeakers()
    monkeypatch.setattr(core.server, "SpeakerID", lambda *a, **k: fake)
    client = TestClient(create_app(PersonaConfig(owner_name="Pat", loved_one_name="Sam"), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt=FakeSTT()))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "This is Pat"})                                    # typed: no voice, no speaker
        while ws.receive_json()["type"] != "reply":
            pass
    assert fake.added == []
