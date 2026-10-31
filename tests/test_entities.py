"""Phase 2 (docs/MEMORY_ARCHITECTURE.md): entities and facts from memories, checked against the memory's own words."""
import time

import pytest

from memory import archive
from memory.entities import Entities
from memory.store import Memory, MemoryStore

SEEDS = {"self": "Pat", "echo": "Echo", "entities": [
    {"name": "Pat", "type": "person", "aliases": ["Pat Smith", "Dad"]},
    {"name": "Sam", "type": "person", "aliases": ["Sammy", "Sam Smith"]},
    {"name": "Echo", "type": "self", "aliases": ["Echo Persona"]}]}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.sqlite")


@pytest.fixture
def ents(store):
    return Entities(store, SEEDS)


def test_seeds_and_aliases(ents):
    assert ents.find("sammy") == ents.find("Sam") == ents.find("SAM SMITH.")
    assert ents.find("Echo Persona") == ents.find("Echo") and ents.find("Tom") is None
    Entities(ents.store, SEEDS)                                          # seeding again changes nothing
    assert len(ents.list()) == 3


def test_only_facts_with_a_real_quote_and_known_predicate_are_kept(store, ents):
    m = store.save(Memory(content="Sam is my wife. Sam is a nurse. Pat has two cats, Biscuit and Pepper."))
    r = ents.apply(m, {"entities": [{"name": "Biscuit", "type": "pet"}, {"name": "Pepper", "type": "pet"}],
                       "facts": [
                           {"subject": "Sam", "predicate": "married_to", "object": "Pat", "quote": "Sam is my wife"},
                           {"subject": "Sam", "predicate": "is_a", "object": "nurse", "quote": "Sam is a nurse"},
                           {"subject": "Biscuit", "predicate": "pet_of", "object": "Pat", "quote": "two cats, Biscuit and Pepper"},
                           {"subject": "Sam", "predicate": "likes", "object": "pizza", "quote": "Sam loves pizza"},  # invented
                           {"subject": "Sam", "predicate": "adores", "object": "Pat", "quote": "Sam is my wife"},  # not on list
                           {"subject": "you", "predicate": "likes", "object": "cats", "quote": "two cats"},           # who?
                       ]}, "test")
    assert r["kept"] == 3 and r["dropped"] == {"quote": 1, "predicate": 1, "subject": 1, "object": 0, "hedge": 0}
    a = ents.about("Sammy")
    got = {(f["subject"], f["predicate"], f["object"]) for f in a["facts"]}
    assert got == {("Sam", "married_to", "Pat"), ("Sam", "is_a", "nurse")}
    assert all(f["status"] == "inferred" and f["confidence"] == 0.8 for f in a["facts"])   # from a confirmed memory
    pat = ents.about("Dad")
    assert {c["name"] for c in pat["connected"]} == {"Sam", "Biscuit"}
    assert ents.entity(ents.find("Biscuit"))["type"] == "pet"
    assert [x["id"] for x in a["memories"]] == [m.id]


def test_note_facts_are_less_sure_and_lead_to_the_conversation(store, ents):
    for i in range(16):
        store.add_turn("user", f"t{i}", "sam")
    store.db.execute("UPDATE turns SET ts=ts-5000"); store.db.commit()
    archive.build_episodes(store)
    n = store.save(Memory(content="On Tuesday, Sam shared that her middle name is Lee.", kind="summary",
                          status="inferred", confidence=0.6, source="auto:conversation-summary", span="turns:1-16"))
    assert "Echo" in ents.prompt_for(n) and "skip facts" in ents.prompt_for(n)
    ents.apply(n, {"facts": [{"subject": "Sam", "predicate": "has_name", "object": "Lee",
                              "quote": "her middle name is Lee"}]}, "test")
    a = ents.about("Sam")
    assert a["facts"][0]["confidence"] == 0.5 and a["facts"][0]["object"] == "Lee"
    assert len(a["episodes"]) == 1 and a["episodes"][0]["first_turn"] == 1


def test_edit_retires_old_facts_and_forget_hides_them(store, ents):
    m = store.save(Memory(content="Pat lives in Springfield, Ohio."))
    assert [x.id for x in ents.pending()] == [m.id]
    ents.apply(m, {"facts": [{"subject": "Pat", "predicate": "lives_in", "object": "Springfield, Ohio",
                              "quote": "lives in Springfield, Ohio"}]}, "test")
    assert ents.pending() == []
    time.sleep(0.01)
    store.correct(m.id, "Pat lives in Dayton, Ohio.")
    assert [x.id for x in ents.pending()] == [m.id]                      # edited → read again
    m2 = store.get(m.id)
    ents.apply(m2, {"facts": [{"subject": "Pat", "predicate": "lives_in", "object": "Dayton, Ohio",
                               "quote": "lives in Dayton, Ohio"}]}, "test")
    assert [f["object"] for f in ents.about("Pat")["facts"]] == ["Dayton, Ohio"]
    assert store.db.execute("SELECT COUNT(*) FROM facts WHERE retired IS NOT NULL").fetchone()[0] == 1   # kept
    store.forget(m.id)
    assert ents.about("Pat")["facts"] == [] and ents.about("Pat")["memories"] == []
    store.restore_hidden(m.id)
    assert len(ents.about("Pat")["facts"]) == 1


def test_unknown_name_returns_none_and_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    import memory.entities as me
    monkeypatch.setattr(me, "load_seeds", lambda path=None: SEEDS)
    s = MemoryStore(tmp_path / "m.sqlite")
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), s))
    assert c.get("/api/about", params={"name": "Sammy"}).json()["entity"]["name"] == "Sam"
    assert c.get("/api/about", params={"name": "Nobody"}).status_code == 404
    assert {e["name"] for e in c.get("/api/entities").json()} == {"Pat", "Sam", "Echo"}


def test_checks_for_the_models_usual_mistakes(store, ents):
    m = store.save(Memory(content="On Thursday I talked with Pat about a new memory system that will allow me to "
                                  "recognize voices. I can blink and smile. You mentioned a trip.", kind="summary",
                          status="inferred", source="auto:conversation-summary"))
    r = ents.apply(m, {"facts": [
        {"subject": "Echo", "predicate": "can", "object": "recognize voices", "quote": "will allow me to recognize voices"},
        {"subject": "Echo", "predicate": "can", "object": "blink and smile", "quote": "I can blink and smile"},
        {"subject": "Pat", "predicate": "has_name", "object": "Pat", "quote": "talked with Pat"},
        {"subject": "Sam", "predicate": "talked_about", "object": "a trip", "quote": "You mentioned a trip"},  # who was "you"?
        {"subject": "Echo", "predicate": "talked_about", "object": "memory system", "quote": "a new memory system"},
    ]}, "test")
    assert r["kept"] == 2 and r["dropped"]["hedge"] == 1 and r["dropped"]["object"] == 1 and r["dropped"]["subject"] == 1
    assert {f["object"] for f in ents.about("Echo")["facts"]} == {"blink and smile", "memory system"}


def test_pet_facts_the_wrong_way_round_are_turned(store, ents):
    m = store.save(Memory(content="Pat has two cats, Biscuit and Pepper."))
    ents.apply(m, {"entities": [{"name": "Biscuit", "type": "pet"}],
                   "facts": [{"subject": "Pat", "predicate": "pet_of", "object": "Biscuit", "quote": "two cats biscuit and pepper"}]}, "t")
    assert [(f["subject"], f["predicate"], f["object"]) for f in ents.about("Biscuit")["facts"]] == [("Biscuit", "pet_of", "Pat")]
