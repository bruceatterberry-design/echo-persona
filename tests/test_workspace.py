"""Stage 1a workspace: observe-only turn records (no message text, 30-day retention, never breaks a reply)."""
import json
import sqlite3
import time

from fastapi.testclient import TestClient

from core import workspace as cws
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


def test_save_and_summary(tmp_path):
    w = cws.WorkspaceLog(tmp_path / "w.sqlite")
    r = cws.TurnRecord(memory_ids=[3, 7])
    r.first_word()
    w.save(r, "llm")
    w.save(cws.TurnRecord(), "browser")
    s = w.summary()
    assert s["turns"] == 2 and s["routes"] == {"llm": 1, "browser": 1}
    rec = w.recent(1)[0]
    assert rec["route"] == "browser" and "t0" not in rec
    assert w.recent(5)[1]["memory_ids"] == [3, 7]


def test_old_records_deleted_after_30_days(tmp_path):
    w = cws.WorkspaceLog(tmp_path / "w.sqlite")
    old = cws.TurnRecord(started=time.time() - 31 * 86400)
    new = cws.TurnRecord()
    w.save(old, "llm")
    w.save(new, "llm")
    w.prune()
    assert w.summary(days=365)["turns"] == 1


def test_save_never_raises(tmp_path):
    w = cws.WorkspaceLog(tmp_path / "missing-dir" / "w.sqlite")   # can't be created
    w.save(cws.TurnRecord(), "llm")                                  # logged, not raised


def chat(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass


def test_llm_turn_recorded_without_text(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    chat(client, "my secret purple giraffe")
    rows = sqlite3.connect(tmp_path / "workspace.sqlite").execute("SELECT route, record FROM turns").fetchall()
    assert len(rows) == 1 and rows[0][0] == "llm"
    rec = json.loads(rows[0][1])
    assert "meaning_search" in rec
    assert rec["llm_calls"] == 1 and rec["speaker"] == "typed" and rec["total_ms"] >= rec["first_ms"] >= 0
    raw = (tmp_path / "workspace.sqlite").read_bytes()
    assert b"giraffe" not in raw and b"purple" not in raw
    assert client.get("/api/workspace").json()["summary"]["routes"] == {"llm": 1}


class BrokenBackend(EchoBackend):
    async def chat(self, messages):
        raise ConnectionError("down")


def test_llm_error_recorded(tmp_path):
    client = TestClient(create_app(PersonaConfig(), BrokenBackend(), MemoryStore(tmp_path / "m.sqlite")))
    chat(client, "hi")
    assert client.get("/api/workspace").json()["recent"][0]["route"] == "llm_error"
