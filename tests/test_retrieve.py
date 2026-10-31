"""Phase 3 (docs/MEMORY_ARCHITECTURE.md): retrieval engine v2 — who/what, when, words, meaning; quiet when nothing fits."""
import time
from datetime import datetime

import pytest

from memory import archive
from memory.entities import Entities
from memory.retrieve import Retriever, format_for_message, time_range
from memory.store import Memory, MemoryStore
from tests.test_entities import SEEDS

NOW = datetime(2026, 10, 31, 1, 0).timestamp()   # a Saturday


def day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize("said,start,end", [
    ("What did we talk about yesterday?", "2026-10-30 00:00", "2026-10-31 00:00"),
    ("what did we talk about on Thursday", "2026-10-29 00:00", "2026-10-30 00:00"),
    ("the day before yesterday", "2026-10-29 00:00", "2026-10-30 00:00"),
    ("on October 27", "2026-10-27 00:00", "2026-10-28 00:00"),
    ("3 days ago", "2026-10-28 00:00", "2026-10-29 00:00"),
    ("last Christmas", "2025-12-25 00:00", "2025-12-26 00:00"),
    ("last Saturday", "2026-10-24 00:00", "2026-10-25 00:00"),
])
def test_time_words(said, start, end):
    a, b = time_range(said, NOW)
    assert (day(a), day(b)) == (start, end)


def test_no_time_words():
    assert time_range("What's my favorite food?", NOW) is None


@pytest.fixture
def world(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    e = Entities(s, SEEDS)
    mems = {
        "wife": s.save(Memory(content="Sam is my wife.")),
        "pie": s.save(Memory(content="Sam's favorite food: she loves her mom's apple pie.", told_by="sam")),
        "cats": s.save(Memory(content="Pat has two cats, Biscuit and Pepper.")),
        "engine": s.save(Memory(content="Pat wants a rotary engine but can't afford it.")),
    }
    facts = {"wife": [("Pat", "married_to", "Sam", "sam is my wife")],
             "pie": [("Sam", "likes", "apple pie", "she loves her mom's apple pie")],
             "cats": [("Pat", "owns", "Biscuit", "two cats biscuit and pepper"), ("Pat", "owns", "Pepper", "two cats biscuit and pepper")],
             "engine": [("Pat", "wants", "a rotary engine", "pat wants a rotary engine")]}
    for k, m in mems.items():
        e.apply(m, {"entities": [{"name": "Biscuit", "type": "pet"}, {"name": "Pepper", "type": "pet"}],
                    "facts": [{"subject": a, "predicate": p, "object": o, "quote": q} for a, p, o, q in facts[k]]}, "t")
    return s, e, Retriever(s, e), mems


def test_my_means_the_speaker_and_facts_answer(world):
    s, e, r, mems = world
    res = r.retrieve("What's my favorite food?", speaker="sam", now=NOW)
    assert [h.memory.id for h in res.hits] == [mems["pie"].id]
    assert res.facts == {"Sam": ["likes apple pie"]}             # only what was asked, for "my"
    assert "Sam" not in r.retrieve("What's my favorite food?", speaker="pat", now=NOW).facts   # not given as Pat's


def test_rare_name_alone_is_enough_frequent_needs_a_topic(world):
    s, e, r, mems = world
    assert [h.memory.id for h in r.retrieve("Tell me about Biscuit.", now=NOW).hits] == [mems["cats"].id]
    for _ in range(5):                                                    # make Pat a frequent name
        e.db.execute("UPDATE entities SET mentions=mentions+10 WHERE name='Pat'")
    assert r.retrieve("When is Pat's birthday?", now=NOW).hits == []   # nothing about birthdays: quiet
    assert [h.memory.id for h in r.retrieve("What engine does Pat want?", now=NOW).hits] == [mems["engine"].id]


def test_facts_read_from_both_sides_and_asked_first(world):
    s, e, r, mems = world
    assert r.retrieve("Who is Sam married to?", now=NOW).facts["Sam"][0] == "married to Pat"
    assert r.facts_for(e.find("Biscuit")) == ["has Pat"] or "pet of Pat" in r.facts_for(e.find("Biscuit"))


def test_time_question_brings_that_days_notes(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    thu = datetime(2026, 10, 29, 9, 0).timestamp()
    for i in range(16):
        s.add_turn("user", f"t{i}")
    s.db.execute("UPDATE turns SET ts=? + id*60", (thu,)); s.db.commit()
    archive.build_episodes(s, now=NOW)
    n = s.save(Memory(content="On Thursday we fixed some code.", kind="summary", status="inferred",
                      source="auto:conversation-summary", span="turns:1-16"))
    r = Retriever(s, Entities(s, SEEDS))
    res = r.retrieve("What did we talk about on Thursday?", now=NOW)
    assert [h.memory.id for h in res.hits] == [n.id] and res.hits[0].why == ["time"]
    assert "Oct 29" in format_for_message(res, s)                       # dated by the conversation, not the note
    assert r.retrieve("What did we talk about last Christmas?", now=NOW).hits == []


def test_hidden_and_excluded_left_out(world):
    s, e, r, mems = world
    s.forget(mems["pie"].id)
    assert r.retrieve("What's my favorite food?", speaker="sam", now=NOW).hits == []
    assert r.retrieve("Tell me about Biscuit.", now=NOW, exclude={mems["cats"].id}).hits == []
