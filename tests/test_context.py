"""Phase 5 (docs/MEMORY_ARCHITECTURE.md): the context pack — people lines + highest-priority memories, size-capped, cached."""
import time

from memory import consolidate
from memory.context import Pack, build
from memory.entities import Entities
from memory.retrieve import Retriever
from memory.store import Memory, MemoryStore
from tests.test_entities import SEEDS


def setup(tmp_path, n=60):
    s = MemoryStore(tmp_path / "m.sqlite")
    e = Entities(s, SEEDS)
    life = s.save(Memory(content="I caught my first fish at Lake Erie.", kind="memory", told_by="pat"))
    wife = s.save(Memory(content="Sam is my wife."))
    e.apply(wife, {"facts": [{"subject": "Pat", "predicate": "married_to", "object": "Sam", "quote": "sam is my wife"}]}, "t")
    for i in range(n):
        s.save(Memory(content=f"Low-importance inferred detail number {i} about something {'x' * (i % 7)}.", status="inferred",
                      confidence=0.4, source="auto:test"))
    consolidate.priorities(s, e, time.time())
    return s, e, Retriever(s, e), life, wife


def test_pack_keeps_important_memories_and_stays_small(tmp_path):
    s, e, r, life, wife = setup(tmp_path)
    text, ids = build(s, e, r, budget=1200)
    assert life.id in ids and wife.id in ids                       # confirmed ones first
    assert len(text) < 1200 + 600 and len(ids) < 62                 # capped, not "everything"
    assert "- Pat: married to Sam" in text and "- Sam: married to Pat" in text
    assert "(inferred, not confirmed)" in text


def test_pack_is_cached_until_memories_change(tmp_path):
    s, e, r, life, wife = setup(tmp_path, n=3)
    p = Pack(s, e, r)
    t1, _ = p.get()
    assert p.get()[0] is t1                                         # same object: not rebuilt
    s.forget(life.id)
    t2, ids = p.get()
    assert life.id not in ids and "first fish" not in t2


def test_reply_prompt_uses_the_pack_and_grounding_rule(tmp_path):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from tests.test_memory import CapturingBackend
    from core.server import create_app
    s, e, r, life, wife = setup(tmp_path, n=3)
    b = CapturingBackend()
    with TestClient(create_app(PersonaConfig(), b, s)).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Hello"})
        while ws.receive_json().get("state") != "idle":
            pass
    system = b.seen[0][0]["content"]
    assert "first fish" in system and "People and pets I know" in system and "made-up examples" in system


def test_pack_holds_still_during_a_conversation(tmp_path):
    s, e, r, life, wife = setup(tmp_path, n=3)
    p = Pack(s, e, r)
    t1, _ = p.get()
    s.save(Memory(content="Pat's favourite song is Moon River.", kind="memory", told_by="pat"))
    assert p.stale() and p.get(refresh=False)[0] is t1                 # mid-conversation: unchanged prompt
    assert "Moon River" in p.get(refresh=True)[0] and not p.stale()     # quiet time: refreshed
