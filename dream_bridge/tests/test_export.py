"""Memory export (protocol section 6) against a small copy of Echo's real schema. Never touches the owner's data."""
import hashlib
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import crypto, envelope, export, service, store
from tests.test_bridge import TOWER_ID, FakeTower, bridge_with

SCHEMA = """
CREATE TABLE memories (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
  confidence REAL NOT NULL, source TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, period TEXT,
  when_text TEXT, year INTEGER, people TEXT, place TEXT, told_by TEXT, hidden REAL, span TEXT);
CREATE VIRTUAL TABLE memories_fts USING fts5(content);
CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, note TEXT,
  mentions INTEGER NOT NULL DEFAULT 0, first_seen REAL, last_seen REAL, created REAL NOT NULL, by TEXT NOT NULL);
CREATE TABLE facts (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, predicate TEXT NOT NULL, object_id INTEGER,
  value TEXT, status TEXT NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL, span TEXT, memory_id INTEGER, created REAL NOT NULL);
CREATE TABLE relations (a_type TEXT NOT NULL, a_id INTEGER NOT NULL, rel TEXT NOT NULL, b_type TEXT NOT NULL, b_id INTEGER NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL, source TEXT, created REAL NOT NULL, PRIMARY KEY(a_type, a_id, rel, b_type, b_id));
"""


@pytest.fixture
def echo(tmp_path, monkeypatch):
    root = tmp_path / "Echo"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir()
    monkeypatch.setattr(store, "ECHO_DIR", root)
    monkeypatch.setattr(store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("DREAM_BRIDGE_DATA", str(tmp_path / "dream"))
    monkeypatch.setattr(service.asyncio, "sleep", _nosleep)
    db = root / "data" / "memory.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    t0 = time.time() - 10000
    rows = [(i, "memory", f"the owner remembers fishing trip number {i} at the lake", t0 + i) for i in range(1, 101)]
    rows += [(101, "summary", "We talked about adding the loved one's students to her IEP list", t0 + 200),
             (102, "memory", "A hidden memory", t0 + 201)]
    for i, kind, content, upd in rows:
        con.execute("INSERT INTO memories (id, kind, content, status, confidence, source, created, updated, people, place, hidden) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (i, kind, content, "confirmed", 0.9, "photo:/secret/path.jpg", upd, upd,
                                                     "the owner", "Lake", t0 if i == 102 else None))
        con.execute("INSERT INTO memories_fts (rowid, content) VALUES (?, ?)", (i, content))
    con.execute("INSERT INTO entities VALUES (1, 'person', 'the owner', NULL, 5, NULL, NULL, 0, 'seed')")
    con.execute("INSERT INTO entities VALUES (2, 'place', 'Lake', NULL, 3, NULL, NULL, 0, 'seed')")
    con.execute("INSERT INTO entities VALUES (3, 'concept', 'student roster', NULL, 1, NULL, NULL, 0, 'auto')")
    for i in range(1, 102):
        con.execute("INSERT INTO relations VALUES ('memory', ?, 'mentions', 'entity', 1, 1.0, 'inferred', NULL, 0)", (i,))
    con.execute("INSERT INTO relations VALUES ('memory', 101, 'mentions', 'entity', 3, 1.0, 'inferred', NULL, 0)")
    con.execute("INSERT INTO relations VALUES ('memory', 5, 'related', 'memory', 6, 0.7, 'inferred', NULL, 0)")
    con.execute("INSERT INTO facts VALUES (1, 1, 'likes', 2, NULL, 'confirmed', 0.9, 'memory:5', 'span', 5, 0)")
    con.commit(); con.close()
    (root / "config" / "music.json").write_text(json.dumps({"approved_genres": ["blues"], "approved_artists": ["Steely Dan"]}))
    return db


async def _nosleep(_s):
    return None


def _hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_read_only_and_bounded(echo):
    before = _hash(echo)
    b = export.build()
    assert len(b["memories"]) == 60 and _hash(echo) == before
    with pytest.raises(sqlite3.OperationalError):
        export._ro(echo).execute("DELETE FROM memories")


def test_private_hidden_and_sources_never_sent(echo):
    out = json.dumps(export.for_sending(export.build(limit=60)))
    allb = [export.build(refs=[f"memory:{i}" for i in range(1, 103)], limit=60) for _ in range(1)]
    ids = {m["id"] for b in allb for m in b["memories"]}
    assert 101 not in ids and 102 not in ids                     # students + hidden
    assert "student" not in out.lower() and "/secret/path" not in out and "source" not in out
    assert "_held_back" not in out
    assert all(e["name"] != "student roster" for e in export.build(refs=["memory:101"])["entities"])


def test_rotation_and_changed_first(echo):
    b1 = export.build(); export.mark_sent(b1)
    b2 = export.build()
    first, second = {m["id"] for m in b1["memories"]}, [m["id"] for m in b2["memories"]]
    assert not set(second[:40]) & first          # the 40 never-sent memories come first ...
    assert len(set(second) & first) == 20        # ... then it tops up with the longest-unsent ones
    export.mark_sent(b2)
    con = sqlite3.connect(echo); con.execute("UPDATE memories SET updated=? WHERE id=7", (time.time() + 5,)); con.commit(); con.close()
    assert export.build()["memories"][0]["id"] == 7               # changed since last sent → goes first


def test_links_only_point_inside_batch(echo):
    b = export.build(refs=["memory:5", "memory:6"])
    mem = {f"memory:{m['id']}" for m in b["memories"]}
    ent = {f"entity:{e['id']}" for e in b["entities"]}
    assert mem == {"memory:5", "memory:6"}
    assert all(r["a"] in mem | ent and r["b"] in mem | ent for r in b["relations"])
    assert {"a": "memory:5", "rel": "related", "b": "memory:6", "weight": 0.7} in b["relations"]
    assert b["facts"][0]["predicate"] == "likes" and b["interests"] == ["music genre: blues", "music artist: Steely Dan"]


def test_query_search(echo):
    b = export.build(query="fishing trip 42", limit=5)
    assert 0 < len(b["memories"]) <= 5


async def _paired(tower):
    b = bridge_with(tower)
    await b.pair_start(); await b.pair_confirm()
    return b


def _request(tower, payload):
    msg = {**envelope.make("memory_request", payload), "source": "dream", "destination": "echo"}
    return msg, crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg)


async def test_memory_request_off_then_on(echo):
    tower = FakeTower()
    b = await _paired(tower)
    c = TestClient(service.lan_app(b))
    msg, box = _request(tower, {"query": "fishing", "limit": 10})
    rep = crypto.unseal(tower.key, "RESP", "/v1/msg", c.post("/v1/msg", json=box).json())
    assert rep["task_type"] == "ack" and "switched off" in rep["payload"]["reason"]
    export.set_enabled(True)
    msg, box = _request(tower, {"query": "fishing", "limit": 10})
    rep = crypto.unseal(tower.key, "RESP", "/v1/msg", c.post("/v1/msg", json=box).json())
    assert rep["task_type"] == "memory_batch" and rep["in_reply_to"] == msg["message_id"]
    assert 0 < len(rep["payload"]["memories"]) <= 10 and "_held_back" not in rep["payload"]


async def test_push_needs_switch_then_delivers(echo):
    tower = FakeTower()
    b = await _paired(tower)
    with pytest.raises(service.HTTPException) as e:
        await b.push_batch()
    assert e.value.status_code == 409 and not [m for m in tower.received if m["task_type"] == "memory_batch"]
    export.set_enabled(True)
    r = await b.push_batch(by="owner")
    sent = [m for m in tower.received if m["task_type"] == "memory_batch"]
    assert r["accepted"] and len(sent) == 1 and len(sent[0]["payload"]["memories"]) == 60
    assert len(export.sent_log()["memories"]) == 60
    assert list((store.dirs()["outbox"]).glob("*-push-*.json"))


def test_generic_tower_request_gets_a_full_batch(echo):
    b = export.build(query="recent experiences, open questions and interests", limit=60)
    assert len(b["memories"]) == 60
