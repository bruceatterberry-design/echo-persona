"""UCA 2.0 Phase 2: live workspace (ECW) + NDAS in shadow; replies must not change; failures must not matter."""
import json
import sqlite3
import time

from fastapi.testclient import TestClient

from core import attention
from core import workspace as cws
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import Memory, MemoryStore


def small_store(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    a, _ = store.add(Memory(content="Tom and I went fishing at the lake.", status="confirmed", confidence=0.9, source="t"))
    b, _ = store.add(Memory(content="Tom fixed the boat motor.", status="confirmed", confidence=0.9, source="t"))
    c, _ = store.add(Memory(content="the loved one loves apple pie.", status="confirmed", confidence=0.9, source="t"))
    db = store.db
    db.execute("INSERT INTO entities(type, name, mentions, created, by) VALUES ('person', 'Tom', 2, 0, 'test')")
    tom = db.execute("SELECT id FROM entities WHERE name='Tom'").fetchone()[0]
    for m in (a, b):
        db.execute("INSERT INTO relations(a_type,a_id,rel,b_type,b_id,weight,status,created) VALUES ('memory',?,'mentions','entity',?,1.0,'confirmed',0)",
                   (m.id, tom))
    db.commit()
    return store, tom, (a.id, b.id, c.id)


def test_entity_activates_its_memories_and_context_carries_over(tmp_path):
    store, tom, (a, b, c) = small_store(tmp_path)
    att = attention.Attention(store)
    r = att.on_message(0.0, [{"id": tom, "how": "named"}], ["executive", "memory"])
    assert set(r["memories"]) == {a, b} and c not in r["memories"] and "memory" in r["systems"]
    att.on_retrieved(0.0, [a])
    follow = att.on_message(10.0, [], ["executive", "language"])          # "what happened there?" names nobody
    assert follow["memories"][0] == a                                      # still in context, strongest
    later = att.on_message(3600.0, [], ["executive"])                      # an hour later it has faded
    assert later["memories"] == []


def test_rebuild_when_memory_changes_keeps_activation(tmp_path):
    store, tom, (a, b, c) = small_store(tmp_path)
    att = attention.Attention(store)
    att.refresh_if_changed()
    att.on_retrieved(0.0, [a])
    assert not att.refresh_if_changed()
    store.add(Memory(content="A new memory.", status="confirmed", confidence=0.9, source="t"))
    assert att.refresh_if_changed() and att.net.potential(f"memory:{a}", 0.0) > 0


def test_live_state_snapshot_has_no_empty_fields_and_bounded_events():
    s = cws.LiveState()
    for i in range(30):
        s.publish(f"e{i}", current_goal="memory")
    snap = s.snapshot()
    assert snap["current_goal"] == "memory" and len(snap["pending_events"]) == cws.ECW_EVENTS and "confidence" not in snap


def chat(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        reply = None
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            if m["type"] == "reply":
                reply = m["text"]
    return reply


def last_record(tmp_path):
    return json.loads(sqlite3.connect(tmp_path / "workspace.sqlite").execute(
        "SELECT record FROM turns ORDER BY id DESC LIMIT 1").fetchone()[0])


def test_ndas_is_retired_from_the_turn(tmp_path, monkeypatch):
    """CA 2.0 Phase 7 (decision D3): NDAS no longer runs in Echo's turn, whatever the old switch says; the workspace is
    still recorded, ids and labels only."""
    texts = ["Do you remember fishing with Tom?", "What happened with the boat?", "Tell me a joke"]
    replies = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("ECHO_NDAS", flag)
        monkeypatch.setattr(attention.Attention, "on_message", lambda *a, **k: 1 / 0)   # never called
        d = tmp_path / flag
        d.mkdir()
        store, *_ = small_store(d)
        client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
        replies[flag] = [chat(client, t) for t in texts]
        rec = last_record(d)
        assert rec["ecw"]["current_goal"] and "replied" in rec["ecw"]["pending_events"]
        assert "ndas" not in rec or rec["ndas"] == {}
        raw = (d / "workspace.sqlite").read_bytes()
        assert b"boat" not in raw and b"fishing" not in raw            # ids and labels only
    assert replies["0"] == replies["1"]
