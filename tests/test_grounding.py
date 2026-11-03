"""Stage 4a: grounding labels from retrieval (no LLM), recorded in the workspace."""
import json
import sqlite3
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory import grounding
from memory.store import MemoryStore


def hit(mid, why, status="confirmed", conf=0.9):
    return NS(memory=NS(id=mid, status=status, confidence=conf), why=why, score=1.0)


def res(hits=(), facts=None):
    return NS(hits=list(hits), facts=facts or {})


def test_labels():
    assert grounding.label(res([hit(1, ["fact"])]), "memory").label == "known"
    assert grounding.label(res([hit(1, ["meaning 0.71", "words a,b"])])).as_record() == \
        {"label": "known", "basis": ["meaning", "words"], "top_id": 1}
    assert grounding.label(res([hit(2, ["about Tom"], status="inferred", conf=0.6)])).label == "believed"
    assert grounding.label(res([hit(3, ["fact"], conf=0.5)])).label == "believed"
    assert grounding.label(res([hit(4, ["with Tom"])])).label == "inferred"
    assert grounding.label(res([hit(5, ["time"])])).label == "inferred"
    assert grounding.label(res(facts={"Tom": ["lives in Ohio"]}), "memory").label == "inferred"
    assert grounding.label(res([hit(6, ["meaning 0.70"])])).label == "inferred"     # loose meaning alone
    assert grounding.label(res([hit(7, ["meaning 0.80"])])).label == "known"
    assert grounding.label(res(), "memory").label == "unknown"
    assert grounding.label(res(), "chat").label == "none"
    assert grounding.label(None, "reasoning").label == "none"


def test_no_names_or_text_in_basis():
    g = grounding.label(res([hit(1, ["about Pat Jones", "words carpet,fire"])]))
    assert g.basis == ["about", "words"]


def test_recorded_in_workspace(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Do you remember our trip to the lake?"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    rec = json.loads(sqlite3.connect(tmp_path / "workspace.sqlite").execute("SELECT record FROM turns").fetchone()[0])
    assert rec["plan"]["kind"] == "memory" and rec["grounding"]["label"] == "unknown"   # empty memory: nothing found
    assert client.get("/api/workspace").json()["summary"]["grounding"] == {"unknown": 1}


class Capture(EchoBackend):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def chat_stream(self, messages):
        self.sent.append(messages[-1]["content"])
        async for p in super().chat_stream(messages):
            yield p


def ask(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass


def test_unknown_note_sent_once_not_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHO_GROUNDING_NOTES", raising=False)
    b, store = Capture(), MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), b, store))
    ask(client, "Do you remember our trip to the lake?")
    assert "My memory check: I found nothing" in b.sent[-1]
    sent = [t for t in store.turns_window(as_sent=True) if t["role"] == "user"]
    assert sent and all("My memory check" not in t["content"] for t in sent)   # never replayed later
    ask(client, "Tell me a joke")                                           # chat, nothing found → no note
    assert "My memory check" not in b.sent[-1]
    rec = json.loads(sqlite3.connect(tmp_path / "workspace.sqlite").execute(
        "SELECT record FROM turns ORDER BY id LIMIT 1").fetchone()[0])
    assert "grounding_unknown" in rec["hints"]


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_GROUNDING_NOTES", "0")
    b = Capture()
    client = TestClient(create_app(PersonaConfig(), b, MemoryStore(tmp_path / "m.sqlite")))
    ask(client, "Do you remember our trip to the lake?")
    assert "My memory check" not in b.sent[-1]


def test_notes():
    assert grounding.note("unknown").startswith("[My memory check")
    assert grounding.note("inferred") == grounding.note("known") == grounding.note("believed") == grounding.note("none") == ""
