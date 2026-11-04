"""Dream reader, conversation pause and the Synthetic Dreams checker."""
import copy
import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import crypto, envelope, reader, service, store, synthetic
from tests.test_bridge import TOWER_ID, FakeTower
from tests.test_export import _paired, echo  # noqa: F401  (fixture)

EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "dream" / "synthetic_dreams_example.json"


async def _noop():
    return None


async def _manual(tower):
    """Paired bridge whose reader only runs when the test calls it (no automatic run after a dream arrives)."""
    b = await _paired(tower)
    b.read_and_report = _noop
    return b, (lambda: service.Bridge.read_and_report(b))


def _prop(pid, refs, quotes, kind="association", change="Link two fishing trips"):
    return {"proposal_id": pid, "kind": kind, "proposed_change": change, "reason": "same lake",
            "supporting_refs": refs, "evidence": [{"ref": r, "quote": q} for r, q in quotes], "confidence": 0.6,
            "consequences": "Echo connects them", "scores": {k: 0.5 for k in reader.WEIGHTS}}


def _send_dream(c, tower, props, did="dream-r1"):
    pl = {"dream_id": did, "model": "test", "narrative": "[Speculation] ...", "proposals": props}
    pl["sha256"] = service.package_sha256(pl)
    msg = {**envelope.make("dream_result", pl), "source": "dream", "destination": "echo"}
    r = c.post("/v1/msg", json=crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg))
    return msg, crypto.unseal(tower.key, "RESP", "/v1/msg", r.json())


async def test_reader_checks_and_queues_without_touching_echo(echo):
    before = hashlib.sha256(echo.read_bytes()).hexdigest()
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    props = [
        _prop("good", ["memory:5", "memory:6"], [("memory:5", "fishing trip number 5")]),
        _prop("badquote", ["memory:5"], [("memory:5", "caught a whale")]),
        _prop("nosuch", ["memory:9999"], []),
        _prop("hidden", ["memory:102"], []),
        _prop("private", ["memory:101"], []),
        _prop("synthetic", ["synthetic:syn-0001:m1"], []),
        _prop("self", ["memory:7"], [], kind="personality", change="Be more playful"),
    ]
    msg, ack = _send_dream(c, tower, props)
    assert ack["payload"]["receipt"]["dream_id"] == "dream-r1"
    await read()
    st = {r["proposal_id"]: r for r in map(dict, reader.db().execute("SELECT proposal_id, status, check_notes, protected FROM proposals"))}
    assert st["good"]["status"] == "pending" and st["self"]["status"] == "pending" and st["self"]["protected"] == 1
    for bad, why in [("badquote", "word for word"), ("nosuch", "no such memory"), ("hidden", "not available"),
                     ("private", "not available"), ("synthetic", "synthetic refs never count")]:
        assert st[bad]["status"] == "invalid" and why in st[bad]["check_notes"], bad
    results = [m for m in tower.received if m["task_type"] == "ack" and m["in_reply_to"] == msg["message_id"]]
    assert len(results) == 1
    got = {r["proposal_id"]: r["accepted"] for r in results[0]["payload"]["results"]}
    assert got == {"good": True, "self": True, "badquote": False, "nosuch": False, "hidden": False, "private": False, "synthetic": False}
    assert hashlib.sha256(echo.read_bytes()).hexdigest() == before          # Echo's memory untouched
    assert not list(store.dirs()["inbox"].glob("*.json"))                      # moved to inbox/read
    assert reader.counts() == {"dreams": 1, "pending": 2, "approved": 0, "rejected": 0, "invalid": 5, "applied": 0, "retired": 0}


async def test_decide_and_history(echo):
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    _send_dream(c, tower, [_prop("p1", ["memory:5"], [])])
    await read()
    ctl = TestClient(service.control_app(b))
    assert ctl.post("/dreams/p1/approve", params={"note": "nice"}).json()["status"] == "approved"
    assert ctl.post("/dreams/p1/reject").status_code == 409                # already decided
    h = reader.db().execute("SELECT action, by FROM history WHERE proposal_id='p1'").fetchall()
    assert [tuple(x) for x in h] == [("approved", "owner")]


async def test_results_wait_if_tower_is_away(echo):
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    _send_dream(c, tower, [_prop("p1", ["memory:5"], [])])
    saved = dict(b.disc.towers)
    b.disc.towers.clear()                                                   # tower switched off right after sending
    await read()
    assert json.loads((store.STATE_DIR / "unsent_results.json").read_text())
    b.disc.towers.update(saved)
    await read()
    assert json.loads((store.STATE_DIR / "unsent_results.json").read_text()) == []
    assert any(m["task_type"] == "ack" and m["payload"].get("results") for m in tower.received)


# ---- conversation pause ----
async def test_conversation_pause_and_resume(echo, monkeypatch):
    tower = FakeTower()
    b = await _paired(tower)
    ctl = TestClient(service.control_app(b))
    assert ctl.post("/conversation/ping").json()["new_conversation"] is True
    calls = {"n": 0}

    async def once(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise service.asyncio.CancelledError
    monkeypatch.setattr(service.asyncio, "sleep", once)
    with pytest.raises(service.asyncio.CancelledError):
        await b.conversation_loop()
    conv = [m["payload"]["active"] for m in tower.received if m["task_type"] == "conversation"]
    assert conv == [True]
    b.last_ping = time.time() - service.CONV_QUIET_S - 1                   # 3 quiet minutes later
    calls["n"] = 0
    with pytest.raises(service.asyncio.CancelledError):
        await b.conversation_loop()
    conv = [m["payload"]["active"] for m in tower.received if m["task_type"] == "conversation"]
    assert conv == [True, False]


# ---- Synthetic Dreams checker ----
def test_example_file_passes(echo):
    r = synthetic.check(EXAMPLE)
    assert r["ok"], r


def _variant(tmp_path, fn):
    d = json.loads(EXAMPLE.read_text())
    fn(d)
    p = tmp_path / "v.json"
    p.write_text(json.dumps(d))
    return synthetic.check(p)


@pytest.mark.parametrize("change,expect", [
    (lambda d: d["records"][0]["proposals"][0].update(supporting_refs=["memory:12"]), "not a seed"),
    (lambda d: d["records"][0].update(narrative="I dream of a garage full of birdhouses and paint, every roof a different color, again and again."), "must start with"),
    (lambda d: d["records"][0]["proposals"][0]["evidence"][0].update(quote="painted the moon"), "word for word"),
    (lambda d: d.update(count=3), "count says"),
    (lambda d: d["records"][0]["seed_memories"][0].update(content=d["records"][0]["seed_memories"][0]["content"] + " the loved one helped."), "real name"),
    (lambda d: d["records"][0]["seed_memories"][0].update(content=d["records"][0]["seed_memories"][0]["content"] + " His students helped."), "students"),
    (lambda d: d["records"][0].update(synthetic=False), "synthetic"),
])
def test_checker_catches(echo, tmp_path, change, expect):
    import sqlite3
    con = sqlite3.connect(echo); con.execute("INSERT INTO entities VALUES (9, 'person', 'the loved one', NULL, 1, NULL, NULL, 0, 'seed')"); con.commit(); con.close()
    r = _variant(tmp_path, change)
    assert not r["ok"] and expect in json.dumps(r["errors"]), r


# ---- rev d: insights from synthetic dreams ----
def _insight(pid, text, **kw):
    return {"proposal_id": pid, "kind": "insight", "insight": text, "reason": "seen in many synthetic dreams",
            "basis": {"synthetic_records": 30, "examples": ["syn-0001"]}, "confidence": 0.6,
            "scores": {k: 0.5 for k in reader.WEIGHTS}, **kw}


def _send_synthetic(c, tower, props, did="syn-result-1"):
    pl = {"dream_id": did, "origin": "synthetic", "model": "test", "narrative": "[Speculation] ...", "proposals": props}
    pl["sha256"] = service.package_sha256(pl)
    msg = {**envelope.make("dream_result", pl), "source": "dream", "destination": "echo"}
    c.post("/v1/msg", json=crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg))
    return msg


class FakeEcho:
    def __init__(self, cap=3):
        self.lessons, self.cap = [], cap

    def post(self, url, json, timeout):
        import httpx
        if len([x for x in self.lessons if x["active"]]) >= self.cap:
            return httpx.Response(409, json={"detail": "Echo already has 3 dream insights; switch one off first"})
        self.lessons.append({**json, "active": True})
        return httpx.Response(200, json={"ok": True})

    def delete(self, url, timeout):
        import httpx
        src = url.rsplit("/", 1)[1]
        for x in self.lessons:
            if x["source"] == src and x["active"]:
                x["active"] = False
                return httpx.Response(200, json={"dropped": True})
        return httpx.Response(200, json={"dropped": False})


async def test_synthetic_insights_checked_and_become_lessons_not_memories(echo, monkeypatch):
    import sqlite3
    import httpx
    con = sqlite3.connect(echo); con.execute("INSERT INTO entities VALUES (9, 'person', 'the loved one', NULL, 1, NULL, NULL, 0, 'seed')"); con.commit(); con.close()
    before = hashlib.sha256(echo.read_bytes()).hexdigest()
    fe = FakeEcho()
    monkeypatch.setattr(httpx, "post", fe.post)
    monkeypatch.setattr(httpx, "delete", fe.delete)
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    good = [_insight(f"g{i}", f"When a promise comes up, I may gently ask whether it happened ({i}).") for i in range(4)]
    bad = [_insight("refs", "A general idea that cites memories.", supporting_refs=["memory:5"]),
           _insight("name", "the loved one likes it when I ask about her day."),
           _insight("long", "x" * 201),
           _insight("past", "I remember that people like birdhouses."),
           {**_insight("kind", "Some association."), "kind": "association"}]
    _send_synthetic(c, tower, good + bad)
    await read()
    st = dict(reader.db().execute("SELECT proposal_id, status FROM proposals").fetchall())
    assert all(st[f"g{i}"] == "pending" for i in range(4))
    assert all(st[x] == "invalid" for x in ("refs", "name", "long", "past", "kind"))
    for i in range(3):
        assert reader.decide(f"g{i}", True)["status"] == "applied"
    with pytest.raises(OverflowError):            # Echo's cap of 3 holds; g3 stays pending
        reader.decide("g3", True)
    assert dict(reader.db().execute("SELECT proposal_id, status FROM proposals").fetchall())["g3"] == "pending"
    assert reader.retire("g0")["status"] == "retired"
    assert reader.decide("g3", True)["status"] == "applied"
    assert [x["source"] for x in fe.lessons if x["active"]] == ["dream-insight:g1", "dream-insight:g2", "dream-insight:g3"]
    assert hashlib.sha256(echo.read_bytes()).hexdigest() == before          # no memory written


async def test_real_dream_cannot_smuggle_insight(echo):
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    _send_dream(c, tower, [_insight("sneaky", "A general idea in a real dream.")])
    await read()
    r = reader.db().execute("SELECT status, check_notes FROM proposals WHERE proposal_id='sneaky'").fetchone()
    assert r["status"] == "invalid" and "only come from synthetic" in r["check_notes"]


async def test_approved_association_becomes_links_in_echo(echo, monkeypatch):
    import httpx
    sent = []

    def post(url, json, timeout):
        sent.append((url.rsplit("/api/", 1)[1], json))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(httpx, "post", post)
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    _send_dream(c, tower, [_prop("a3", ["memory:5", "memory:6", "memory:7"], [("memory:5", "fishing trip number 5")]),
                           _prop("a1", ["memory:8"], [])])
    await read()
    assert reader.decide("a3", True)["status"] == "applied"
    assert [(u, j["a"], j["b"]) for u, j in sent] == [("memory/dream_link", 5, 6), ("memory/dream_link", 6, 7)]
    assert sent[0][1]["source"] == "dream:dream-r1:a3"
    assert reader.decide("a1", True)["status"] == "approved"          # one memory: nothing to link
    assert len(sent) == 2


# ---- agreed with the tower 2026-11-03 (TOWER_TO_MAC_3) ----
async def test_quote_may_come_from_people_and_place(echo):
    tower = FakeTower()
    b, read = await _manual(tower)
    c = TestClient(service.lan_app(b))
    _send_dream(c, tower, [_prop("pp", ["memory:5"], [("memory:5", "at the lake the owner Lake")])])
    await read()
    assert reader.db().execute("SELECT status FROM proposals WHERE proposal_id='pp'").fetchone()[0] == "pending"


def test_new_blocked_words():
    from bridge import export
    for w in ("two pupils", "the classrooms", "IEPs", "a pupil"):
        assert export.private(w), w


def test_ratio_rules_are_errors_from_ten_records(echo, tmp_path):
    d = json.loads(EXAMPLE.read_text())
    good = d["records"][0]
    recs = []
    for i in range(10):
        r = copy.deepcopy(d["records"][1] if i < 4 else good)        # 4 of 10 flawed = 40%
        sid = f"syn-{i + 1:04d}"
        s = json.dumps(r).replace(r["synthetic_id"], sid)
        recs.append(json.loads(s))
    d.update(records=recs, count=10)
    p = tmp_path / "ten.json"
    p.write_text(json.dumps(d))
    r = synthetic.check(p)
    assert not r["ok"] and any("flawed records" in e for e in r["errors"])
    assert synthetic.check(EXAMPLE)["ok"]                               # 2 records: only a warning
