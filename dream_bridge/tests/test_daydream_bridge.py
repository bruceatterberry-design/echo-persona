"""Protocol rev e (2026-11-04): daydream_activity (Mac→tower, never task text) and daydream_request → daydream_draft."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from bridge import crypto, envelope, service
from tests.test_bridge import TOWER_ID, FakeTower, bridge_with, env  # noqa: F401  (env is a fixture)


class DraftingTower(FakeTower):
    def __init__(self, answer="ok", **kw):
        super().__init__(**kw)
        self.answer = answer

    def handler(self, req):
        if req.url.path == "/v1/msg" and self.key:
            import json
            m = crypto.unseal(self.key, "POST", "/v1/msg", json.loads(req.content))
            if m["task_type"] == "daydream_request":
                self.received.append(m)
                pl = m["payload"]
                if self.answer == "ack":   # an old tower that doesn't know rev e
                    rep = envelope.make("ack", {"accepted": False, "reason": "unknown task_type"}, in_reply_to=m["message_id"])
                else:
                    rep = envelope.make("daydream_draft", {"daydream_id": pl["daydream_id"], "status": self.answer,
                                                           "drafts": [f"tower: {p}" for p in pl["parts"]] if self.answer == "ok" else [],
                                                           "model": "gemma-4-12B", "ms": 1234, "tokens": {"prompt": 50, "reply": 80}},
                                        in_reply_to=m["message_id"])
                rep |= {"source": "dream", "destination": "echo"}
                return httpx.Response(200, json=crypto.seal(self.key, TOWER_ID, "RESP", "/v1/msg", rep))
        return super().handler(req)


async def paired(tower):
    b = bridge_with(tower)
    await b.pair_start()
    await b.pair_confirm()
    return b


async def test_activity_sends_only_known_fields(env):
    tower = FakeTower()
    b = await paired(tower)
    r = b.daydream_activity({"daydream_id": "d1", "seq": 2, "stage": "drafts", "active": True, "simulated": True,
                             "helpers": {"claude": "working"}, "task": "SECRET TASK TEXT", "answer": "x"})
    assert r == {"queued": True}
    await b.activity_task
    sent = [m for m in tower.received if m["task_type"] == "daydream_activity"]
    assert len(sent) == 1
    pl = sent[0]["payload"]
    assert pl["stage"] == "drafts" and pl["helpers"] == {"claude": "working"} and pl["at"]
    assert "task" not in pl and "answer" not in pl and "SECRET" not in str(sent[0])


async def test_activity_rejects_unknown_stage(env):
    b = await paired(FakeTower())
    with pytest.raises(HTTPException):
        b.daydream_activity({"daydream_id": "d1", "stage": "dancing"})


def test_activity_unpaired_is_quiet(env):
    b = bridge_with(FakeTower())
    assert b.daydream_activity({"daydream_id": "d1", "stage": "enter"}) == {"queued": False, "reason": "not paired"}


async def test_request_gets_drafts(env):
    tower = DraftingTower()
    b = await paired(tower)
    r = await b.daydream_request({"daydream_id": "d2", "task": "t", "parts": ["a", "b"]})
    assert r["status"] == "ok" and r["drafts"] == ["tower: a", "tower: b"] and r["model"] == "gemma-4-12B"
    assert tower.received[-1]["payload"]["max_tokens"] == 800


@pytest.mark.parametrize("answer,status", [("busy", "busy"), ("ack", "unavailable")])
async def test_request_busy_or_old_tower(env, answer, status):
    b = await paired(DraftingTower(answer=answer))
    r = await b.daydream_request({"daydream_id": "d3", "task": "t", "parts": ["a"]})
    assert r["status"] == status and r["drafts"] == []


async def test_request_tower_gone(env):
    b = await paired(FakeTower())
    b.disc.towers.clear()
    r = await b.daydream_request({"daydream_id": "d4", "task": "t", "parts": ["a"]})
    assert r["status"] == "unavailable" and "ConnectionError" in r["reason"]


async def test_request_waits_past_the_default_timeout(env):
    tower = DraftingTower()
    b = await paired(tower)
    seen = {}
    orig = b.http.post

    async def post(url, **kw):
        seen["timeout"] = kw.get("timeout")
        return await orig(url, **kw)
    b.http.post = post
    await b.daydream_request({"daydream_id": "d5", "task": "t", "parts": ["a"], "deadline_s": 90})
    assert seen["timeout"] == 100      # 10 s (the heartbeat default) cut real drafts off on 11-04
