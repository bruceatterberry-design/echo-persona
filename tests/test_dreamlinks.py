"""Dream links: understanding between EXISTING memories, never new memories; at most one in recall; fade when unused."""
import time

import pytest

from memory import dreamlinks
from memory.entities import Entities
from memory.retrieve import Retriever, format_for_message
from memory.store import Memory, MemoryStore
from tests.test_entities import SEEDS


@pytest.fixture
def world(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    e = Entities(s, SEEDS)
    m = {
        "lake": s.save(Memory(content="the owner went fishing at Lake Cumberland with his father every summer.")),
        "boat": s.save(Memory(content="The old aluminum boat had a dented bow and a loud outboard motor.")),
        "pie": s.save(Memory(content="the loved one loves her mom's apple pie.")),
    }
    return s, Retriever(s, e), m


def counts(s):
    return {t: s.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("memories", "facts", "entities")}


def test_link_adds_understanding_not_memories(world):
    s, r, m = world
    before = counts(s)
    dreamlinks.add(s.db, m["lake"].id, m["boat"].id, "dream:d1:p1")
    assert counts(s) == before                                       # no memory, fact or person created
    v = s.db.execute("SELECT item_type, action FROM versions WHERE item_type='dream_link'").fetchall()
    assert [tuple(x) for x in v] == [("dream_link", "create")]


def test_only_existing_visible_memories(world):
    s, r, m = world
    with pytest.raises(ValueError):
        dreamlinks.add(s.db, m["lake"].id, 9999, "dream:x")
    with pytest.raises(ValueError):
        dreamlinks.add(s.db, m["lake"].id, m["lake"].id, "dream:x")
    s.db.execute("UPDATE memories SET hidden=? WHERE id=?", (time.time(), m["boat"].id)); s.db.commit()
    with pytest.raises(ValueError):
        dreamlinks.add(s.db, m["lake"].id, m["boat"].id, "dream:x")


def test_recall_brings_one_linked_memory_labelled(world):
    s, r, m = world
    q = "Tell me about fishing at Lake Cumberland"
    plain = r.retrieve(q, k=5)
    assert [h.memory.id for h in plain.hits] == [m["lake"].id]          # the boat isn't found by the words alone
    dreamlinks.add(s.db, m["lake"].id, m["boat"].id, "dream:d1:p1")
    res = r.retrieve(q, k=5)
    assert [h.memory.id for h in res.hits] == [m["lake"].id, m["boat"].id]
    assert res.hits[1].why[0].startswith("dream link")
    assert "link I made while reflecting" in format_for_message(res, s)
    assert s.db.execute("SELECT uses FROM dream_links").fetchone()[0] == 1   # use keeps it alive


def test_never_pushes_out_a_real_match(world):
    s, r, m = world
    dreamlinks.add(s.db, m["lake"].id, m["boat"].id, "dream:d1:p1")
    res = r.retrieve("Tell me about fishing at Lake Cumberland", k=1)
    assert [h.memory.id for h in res.hits] == [m["lake"].id]


def test_fades_when_unused_and_can_be_removed(world):
    s, r, m = world
    t0 = time.time()
    dreamlinks.add(s.db, m["lake"].id, m["boat"].id, "dream:d1:p1", now=t0)
    assert dreamlinks.neighbours(s.db, [m["lake"].id], now=t0 + 29 * 86400)        # a month: 0.25, still there
    assert not dreamlinks.neighbours(s.db, [m["lake"].id], now=t0 + 45 * 86400)    # quiet below 0.2
    assert dreamlinks.remove(s.db, m["lake"].id, m["boat"].id)
    assert not dreamlinks.neighbours(s.db, [m["lake"].id])
    assert [x["status"] for x in dreamlinks.listing(s.db)] == ["removed"]            # history kept


def test_no_links_no_change(world):
    s, r, m = world
    res = r.retrieve("Tell me about fishing at Lake Cumberland", k=5)
    assert all(not w.startswith("dream") for h in res.hits for w in h.why)
