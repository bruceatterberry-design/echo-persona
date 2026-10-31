"""Phase 4 (docs/MEMORY_ARCHITECTURE.md): consolidation — links, repeats, fact contradictions, patterns, priority, guard."""
import json
import time

import numpy as np
import pytest

from memory import archive, consolidate
from memory.entities import Entities
from memory.store import Memory, MemoryStore
from tests.test_entities import SEEDS


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.sqlite")


def vec(store, m, v):
    store.set_vector(m, "test", np.asarray(v, dtype=np.float32))


def test_links_and_repeats_are_marked_not_hidden(store):
    a = store.save(Memory(content="the loved one loves her mom's apple pie."))
    b = store.save(Memory(content="Sam's favourite food is apple pie."))
    c = store.save(Memory(content="the owner talked about pies and dinner."))
    d = store.save(Memory(content="The rotary engine idea."))
    vec(store, a, [1, 0, 0]); vec(store, b, [0.99, 0.1, 0]); vec(store, c, [0.8, 0.6, 0]); vec(store, d, [0, 0, 1])
    links, reps = consolidate.link(store, "test")
    assert reps == 1 and links >= 1
    assert consolidate.repeats(store)[0]["a"]["id"] == a.id and store.get(b.id)          # still in use
    assert {r["memory"]["id"] for r in consolidate.related(store, c.id)} >= {a.id}
    assert consolidate.related(store, d.id) == []
    assert consolidate.link(store, "test") == (links, reps)                                # recomputed, not doubled


def test_one_answer_facts_that_differ_go_to_review(store):
    e = Entities(store, SEEDS)
    a = store.save(Memory(content="the owner grew up in Springfield, Ohio."))
    b = store.save(Memory(content="the owner grew up in Memphis."))
    c = store.save(Memory(content="the owner is from Springfield."))
    for m, place in ((a, "Springfield, Ohio"), (b, "Memphis"), (c, "Springfield")):
        e.apply(m, {"facts": [{"subject": "the owner", "predicate": "from", "object": place,
                               "quote": m.content.lower().rstrip(".")[6:]}]}, "t")
    asked = []
    judge = lambda x, y: (asked.append((x, y)) or (("Memphis" in x + y), "different hometown"))
    judged, flagged = consolidate.fact_conflicts(store, judge)
    assert flagged == 2 and judged == 2                     # Springfield vs Springfield, Ohio is just more detail: not asked
    assert len(store.open_conflicts()) == 2
    assert consolidate.fact_conflicts(store, judge) == (0, 0)   # never asks about the same pair twice
    assert consolidate.fact_conflicts(store, None) == (0, 0)


def test_patterns_from_topics_in_three_conversations_once(store):
    e = Entities(store, SEEDS)
    t0 = time.time() - 30 * 86400
    for k in range(3):                                      # three conversations, a day apart, each with a note
        for i in range(16):
            store.add_turn("user", f"c{k} t{i}")
        store.db.execute("UPDATE turns SET ts=? + id WHERE id > ?", (t0 + k * 86400, k * 16)); store.db.commit()
        n = store.save(Memory(content=f"On day {k} we talked about the weather and item {k}.", kind="summary",
                              status="inferred", source="auto:conversation-summary", span=f"turns:{k * 16 + 1}-{k * 16 + 16}"))
        e.apply(n, {"facts": [{"subject": "Echo", "predicate": "talked_about", "object": "the weather",
                               "quote": "we talked about the weather"}]}, "t")
    archive.build_episodes(store)
    assert consolidate.patterns(store) == ["We often talk about the weather."]
    p = [m for m in store.all() if m.source == "auto:pattern"][0]
    assert p.status == "inferred" and p.kind == "behavior"
    store.forget(p.id)
    assert consolidate.patterns(store) == []                 # forgotten stays forgotten


def test_priority_ranks_confirmed_life_memories_over_notes(store):
    e = Entities(store, SEEDS)
    life = store.save(Memory(content="I caught my first fish at the lake.", kind="memory", told_by="owner"))
    note = store.save(Memory(content="On Tuesday we talked about the sky.", kind="summary", status="inferred"))
    store.note_use([note.id] * 1)
    assert consolidate.priorities(store, e, time.time()) == 2
    score = dict(store.db.execute("SELECT item_id, score FROM priority").fetchall())
    assert score[life.id] > score[note.id]


def test_guard_stops_when_something_went_missing(store):
    e = Entities(store, SEEDS)
    store.save(Memory(content="One."))
    store.add_turn("user", "hello")
    r = consolidate.run(store, e, None)
    assert r["ok"] and r["counts"]["memories"] == 1 and r["check"] == "ok"
    store.db.execute("DELETE FROM turns"); store.db.commit()        # something deleted behind Echo's back
    r = consolidate.run(store, e, None)
    assert not r["ok"] and "turns dropped from 1 to 0" in r["problems"][0]
    assert json.loads(store.get_meta("consolidation_problem"))["ok"] is False


def test_api_and_status(tmp_path):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    s = MemoryStore(tmp_path / "m.sqlite")
    s.save(Memory(content="Something to keep."))
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), s))
    r = c.post("/api/memory/consolidate").json()
    assert r["ok"] and c.get("/api/memory/consolidation").json()["last"]["counts"]["memories"] == 1
    assert c.get("/api/memory/repeats").json() == [] and c.get("/api/memory/1/related").json() == []


def test_upkeep_never_snapshots_a_database_that_isnt_echos_own(tmp_path):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    cfg = PersonaConfig(memory_db=str(tmp_path / "echo.sqlite"), backup_dir=str(tmp_path / "backups"))
    other = MemoryStore(tmp_path / "other.sqlite")
    TestClient(create_app(cfg, EchoBackend(), other)).post("/api/memory/consolidate")
    assert not (tmp_path / "backups").exists()
    own = MemoryStore(tmp_path / "echo.sqlite")
    own.save(Memory(content="Keep me."))
    TestClient(create_app(cfg, EchoBackend(), own)).post("/api/memory/consolidate")
    assert list((tmp_path / "backups").glob("memory-daily-*.sqlite.gz"))
