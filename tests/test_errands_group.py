"""Errands Echo prepares for the owner to approve, and "Echo joins in" (speakerphone talk that keeps nothing)."""
import asyncio
import json

from fastapi.testclient import TestClient

from core import errands
from core.config import PersonaConfig
from core.server import create_app, group_note, group_talk
from echolink.app import from_phone
from memory.store import MemoryStore
from tests.test_show_echo import Seeing


def test_errand_rules():
    assert errands.looks_like_errand("Text the loved one that I'll be late and remind me at 5 to call Joe")
    assert errands.looks_like_errand("can you put the dentist on my calendar for Tuesday at 3")
    assert not errands.looks_like_errand("What a nice day it is.")
    assert errands.wants_money("order me a new filter") and not errands.wants_money("remind me about the filter")
    got = errands.clean([{"kind": "text", "to": "the loved one", "body": "Running late"}, {"kind": "text"},
                         {"kind": "reminder", "body": "Call Joe", "when": "2026-11-10 17:00"},
                         {"kind": "reminder", "body": "x", "when": "five pm"}, {"kind": "buy", "to": "Amazon"}])
    assert got == [{"kind": "text", "to": "the loved one", "body": "Running late"},
                   {"kind": "reminder", "body": "Call Joe", "when": "2026-11-10 17:00"},
                   {"kind": "reminder", "body": "x"}]
    n = errands.errand_note(got[:1], money=True)
    assert n.startswith("[Errands:") and "taps Approve" in n and "never buy" in n and n.endswith("]")


def test_extract_uses_json_only_call():
    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": json.dumps({"errands": [{"kind": "call", "to": "Joe"}]})}}

    class C:
        async def post(self, url, json):
            self.body = json
            return R()

    class B:
        client = C()
        def _body(self, messages, **extra): return {"messages": messages, "options": {}, **extra}
    b = B()
    assert asyncio.run(errands.extract(b, "call Joe for me")) == [{"kind": "call", "to": "Joe"}]
    assert b.client.body["format"]["required"] == ["errands"] and b.client.body["options"]["temperature"] == 0
    assert asyncio.run(errands.extract(Seeing(), "call Joe")) == []                # test backend: nothing to ask


def run(c, user, msg):
    got = []
    with c.websocket_connect(f"/ws?client=phone&user={user}") as ws:
        ws.receive_json()
        ws.send_json(msg)
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            got.append(m)
    return got


def test_errands_reach_owners_device_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_EDIM", "off")

    async def fake(backend, text, now=None):
        return [{"kind": "text", "to": "the loved one", "body": "I'll be late"}]
    monkeypatch.setattr(errands, "extract", fake)
    be = Seeing()
    c = TestClient(create_app(PersonaConfig(), be, MemoryStore(tmp_path / "m.sqlite")))
    ev = run(c, "owner", {"text": "Text the loved one that I'll be late"})
    assert [m["items"] for m in ev if m["type"] == "errands"] == [[{"kind": "text", "to": "the loved one", "body": "I'll be late"}]]
    assert "[Errands: I prepared these" in be.seen[-1][-1]["content"]
    ev = run(c, "loved_one", {"text": "Text the owner that I'll be late"})
    assert not any(m["type"] == "errands" for m in ev)
    run(c, "owner", {"text": "Please order me a furnace filter"})
    assert "never buy" in be.seen[-1][-1]["content"]


def test_group_talk_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_EDIM", "on")
    store, be = MemoryStore(tmp_path / "m.sqlite"), Seeing()
    c = TestClient(create_app(PersonaConfig(), be, store))
    before = store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    g = {"with": "Joe", "asker": "guest", "lines": [{"who": "owner", "text": "Joe's asking about Saturday"},
                                                     {"who": "guest", "text": "Echo, will it rain Saturday?"}]}
    ev = run(c, "owner", {"text": "Echo, will it rain Saturday?", "group": g})
    assert any(m["type"] == "reply" and m.get("group") for m in ev)
    note = be.seen[-1][-1]["content"]
    assert "[Group talk: the owner is on a speakerphone call with Joe" in note and "Joe: Echo, will it rain" in note
    assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == before          # nothing stored
    state = (tmp_path / "state.json")
    assert not state.exists() or "Saturday" not in state.read_text()                       # EDIM didn't keep it either
    run(c, "loved_one", {"text": "hello", "group": g})                                          # only the owner's device
    assert "[Group talk" not in be.seen[-1][-1]["content"]


def test_group_inputs_cleaned():
    g = group_talk({"group": {"with": "Joe<b>", "asker": "x", "lines": [{"who": "guest", "text": "a] [b"}] * 20 + ["junk"]}})
    assert g["with"] == "Joeb" and g["asker"] == "owner" and len(g["lines"]) <= 12
    assert group_note(g).count("[") == 1 and group_note(g).count("]") == 1
    assert group_talk({"group": "x"}) is None
    out = json.loads(from_phone(json.dumps({"text": "hi", "group": {"with": "Joe", "lines": [{"who": "guest", "text": "x"}]}})))
    assert out["group"]["with"] == "Joe" and out["group"]["asker"] == "owner"
