"""The Dream Bridge endpoints must accept a JSON body over real HTTP (they 422'd for days when the
request models lived inside create_app under `from __future__ import annotations`)."""
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import Memory, MemoryStore


def _client(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    return TestClient(create_app(PersonaConfig(), EchoBackend(), s)), s


def test_insight_post_accepts_json_body_and_drop(tmp_path):
    c, _ = _client(tmp_path)
    r = c.post("/api/lessons/insight", json={"text": "I respect a 'not yet' when someone is hesitant.",
                                             "source": "dream-insight:abc", "approved_by": "echo"})
    assert r.status_code == 200, r.text
    assert c.post("/api/lessons/insight", json={"text": "short", "source": "dream-insight:x"}).status_code == 422
    assert c.delete("/api/lessons/insight/dream-insight:abc").json()["dropped"]


def test_dream_link_post_accepts_json_body(tmp_path):
    c, s = _client(tmp_path)
    a = s.save(Memory(content="the owner went fishing at Lake Cumberland with his father every summer."))
    b = s.save(Memory(content="The old aluminum boat had a dented bow and a loud outboard motor."))
    r = c.post("/api/memory/dream_link", json={"a": a.id, "b": b.id, "source": "dream:t"})
    assert r.status_code == 200, r.text
    assert s.db.execute("SELECT count(*) FROM dream_links").fetchone()[0] == 1
