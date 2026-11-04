"""Daydream step 1: trigger words, the developer-only gate, plain-Echo fallback, and the chat socket flow."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from core import daydream
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


@pytest.mark.parametrize("text,task", [
    ("daydream", ""), ("Daydream.", ""), ("Echo, daydream about our budget for next year", "our budget for next year"),
    ("let's daydream: plan a week of meals", "plan a week of meals"), ("okay echo day dream", ""),
    ("Day-dream on how to fix the fence", "how to fix the fence")])
def test_start_words(text, task):
    assert daydream.parse_start(text) == (True, task)


@pytest.mark.parametrize("text", ["I had a daydream at work", "what is a daydream?", "do you ever daydream", ""])
def test_not_a_start(text):
    assert daydream.parse_start(text)[0] is False


@pytest.mark.parametrize("text,stop", [("wake up", True), ("Echo, wake up!", True), ("stop daydreaming", True),
                                       ("Wake up, Echo.", True), ("I wake up at six", False),
                                       ("wake up early tomorrow and walk the dog", False)])
def test_stop_words(text, stop):
    assert daydream.is_stop(text) is stop


def test_gate(tmp_path):
    p = tmp_path / "echo.yaml"
    assert not daydream.available(daydream.load_config(p), "ABC")            # no file: off
    p.write_text("daydream:\n  enabled: true\n  owner_machine: ABC\n")
    c = daydream.load_config(p)
    assert daydream.available(c, "ABC")
    assert not daydream.available(c, "XYZ")                                  # an installed copy elsewhere: hidden
    assert not daydream.available(c, "")                                     # id unreadable: hidden
    p.write_text("daydream:\n  enabled: false\n  owner_machine: ABC\n")
    assert not daydream.available(daydream.load_config(p), "ABC")
    assert c["tower"]["timeout_s"] == 90                                     # defaults fill in the rest


def fake_ollama(log):
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        log.append(body)
        prompt = body["messages"][-1]["content"]
        if "Split this task" in prompt:
            out = json.dumps({"parts": ["part A", "part B"]})
        elif "Work on this part" in prompt:
            out = "draft for " + ("A" if "part A" in prompt else "B")
        else:
            out = "Here's what I think, love."
        return httpx.Response(200, json={"message": {"content": out}, "prompt_eval_count": 10, "eval_count": 5})
    return httpx.MockTransport(handler)


def local(log, instructions="I am Echo."):
    return daydream.Local("http://ollama", "m", {}, lambda: instructions, transport=fake_ollama(log))


def test_plain_echo_when_no_helpers():
    log = []
    res = asyncio.run(daydream.run("plan a trip", local(log), daydream.DEFAULTS))
    assert res.ok and res.answer == "Here's what I think, love."
    assert res.parts == ["part A", "part B"]
    assert res.drafts["echo"] == ["draft for A", "draft for B"]
    assert log[-1]["messages"][0]["content"] == "I am Echo."               # the answer uses Echo's own instructions
    assert res.tokens == {"prompt": 40, "reply": 20}


def test_helpers_offline_failing_or_partial():
    async def offline(task, parts):
        return None

    async def broken(task, parts):
        raise RuntimeError("tower gone")

    async def half(task, parts):
        return ["tower draft A"]

    log = []
    res = asyncio.run(daydream.run("t", local(log), daydream.DEFAULTS,
                                   {"tower": half, "claude": offline, "other": broken}))
    assert res.ok
    assert res.helpers == {"tower": "used", "claude": "unavailable", "other": "failed", "echo": "used"}
    assert res.drafts["tower"] == ["tower draft A", None]
    assert res.drafts["echo"] == [None, "draft for B"]                      # Echo fills only the missing part
    merge = log[-1]["messages"][-1]["content"]
    assert "Draft (tower): tower draft A" in merge and "Draft (echo): draft for B" in merge


def test_bad_plan_falls_back_to_one_part():
    def handler(req):
        return httpx.Response(200, json={"message": {"content": "not json"}})
    loc = daydream.Local("http://o", "m", {}, lambda: "", transport=httpx.MockTransport(handler))
    res = asyncio.run(daydream.run("the task", loc, daydream.DEFAULTS))
    assert res.parts == ["the task"] and res.ok


def test_echo_model_down_never_raises():
    def handler(req):
        raise httpx.ConnectError("down")
    loc = daydream.Local("http://o", "m", {}, lambda: "", transport=httpx.MockTransport(handler))
    res = asyncio.run(daydream.run("t", loc, daydream.DEFAULTS))
    assert res.ok is False and res.answer == ""


def until(ws, kind):
    while True:
        m = ws.receive_json()
        if m["type"] == kind:
            return m


def on(tmp_path, monkeypatch):
    p = tmp_path / "echo.yaml"
    p.write_text("daydream:\n  enabled: true\n  owner_machine: ME\n")
    monkeypatch.setattr(daydream, "CONFIG", p)
    monkeypatch.setattr(daydream, "machine_id", lambda: "ME")


def test_socket_hidden_when_off(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream"})
        m = ws.receive_json()
        while m["type"] not in ("reply", "daydream"):
            m = ws.receive_json()
        assert m["type"] == "reply" and "daydream" in m["text"] and m["text"] != daydream.ENTER_ASK   # an ordinary word: no hint, no mode


def test_socket_enter_ask_and_wake_up(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert until(ws, "daydream")["state"] == "idle"                      # the window shows "Daydream off" + hint
        ws.send_json({"text": "daydream"})
        assert until(ws, "reply")["text"] == daydream.ENTER_ASK
        assert until(ws, "daydream")["state"] == "waiting"
        ws.send_json({"text": "wake up"})
        assert until(ws, "reply")["text"] == daydream.LEAVE_STOPPED
        assert until(ws, "daydream")["state"] == "idle"
        ws.send_json({"text": "hello"})                                      # back to normal replies
        assert until(ws, "reply")["text"].endswith("hello")


def test_socket_task_runs_and_leaves(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)

    async def fake_run(task, loc, cfg, *a, **k):
        return daydream.Result(task=task, answer="Thought it through.", ok=True, parts=[task])
    monkeypatch.setattr(daydream, "run", fake_run)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream: compare two phone plans"})
        assert until(ws, "reply")["text"] == daydream.ENTER
        assert until(ws, "daydream")["state"] == "working"
        r = until(ws, "reply")
        assert r["text"].startswith("Thought it through.") and r["text"].endswith(daydream.LEAVE)
        assert until(ws, "daydream")["state"] == "idle"


def test_socket_failure_says_so(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(daydream, "run", boom)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream about the taxes"})
        until(ws, "reply")
        assert until(ws, "reply")["text"] == daydream.FAILED


# ---- step 2: Claude drafts + critique (no real Claude call) ----
class FakeHelper:
    def __init__(self, allowed=True, drafts=None, critique=None):
        self.ok, self.drafts, self.critique, self.prompts = allowed, drafts, critique, []

    def allowed(self, job):
        return self.ok and job == "daydream"

    def ask(self, job, prompt, schema=None, timeout=None):
        assert job == "daydream"
        self.prompts.append(prompt)
        return self.drafts if "drafts" in schema["properties"] else self.critique


def test_claude_drafts_and_critique():
    h = FakeHelper(drafts={"drafts": ["claude A", "claude B"]},
                   critique={"issues": [{"part": 1, "draft": "claude", "kind": "invented", "note": "made up a date"}],
                             "summary": "B is fine"})
    log = []
    res = asyncio.run(daydream.run("plan a trip", local(log, "I am Echo. I remember the loved one's birthday."),
                                   daydream.DEFAULTS, {"claude": daydream.claude_drafter(h)}, daydream.claude_critic(h)))
    assert res.ok and res.helpers == {"claude": "used", "critic": "used"}
    assert res.drafts == {"claude": ["claude A", "claude B"]}          # Claude covered every part: no Echo drafts
    assert res.issues[0]["kind"] == "invented"
    merge = log[-1]["messages"][-1]["content"]
    assert "made up a date" in merge and "Summary: B is fine" in merge
    for p in h.prompts:                                                 # privacy: only the task and its parts/drafts
        assert "plan a trip" in p and "the loved one" not in p and "I am Echo" not in p


def test_claude_capped_or_off_carries_on():
    h = FakeHelper(allowed=False)
    log = []
    res = asyncio.run(daydream.run("t", local(log), daydream.DEFAULTS, {"claude": daydream.claude_drafter(h)},
                                   daydream.claude_critic(h)))
    assert res.ok and res.helpers == {"claude": "unavailable", "echo": "used", "critic": "unavailable"}
    assert res.drafts["echo"] == ["draft for A", "draft for B"] and h.prompts == []


def test_claude_fails_mid_call():
    h = FakeHelper(drafts=None, critique=None)                         # ask() returned None: timeout, limit, offline
    res = asyncio.run(daydream.run("t", local([]), daydream.DEFAULTS, {"claude": daydream.claude_drafter(h)},
                                   daydream.claude_critic(h)))
    assert res.ok and res.helpers["claude"] == "unavailable" and "echo" in res.drafts


def test_tokens_counted_per_daydream():
    loc = local([])
    asyncio.run(daydream.run("t", loc, daydream.DEFAULTS))
    res = asyncio.run(daydream.run("t", loc, daydream.DEFAULTS))
    assert res.tokens == {"prompt": 40, "reply": 20}


def test_daydream_job_is_off_by_default(tmp_path):
    from core.claude_helper import DEFAULT
    assert DEFAULT["jobs"]["daydream"] is False and DEFAULT["daydream_context"] is False


def test_final_check_revises_a_wrong_answer():
    calls = []

    async def checker(task, answer):
        calls.append(answer)
        return {"issues": [{"part": 0, "draft": "answer", "kind": "mistake", "note": "250 x 8 is 2000, not 3000"}],
                "summary": "maths wrong"}
    log = []
    res = asyncio.run(daydream.run("t", local(log), daydream.DEFAULTS, checker=checker))
    assert calls == ["Here's what I think, love."] and res.revised and res.final_issues[0]["kind"] == "mistake"
    assert "250 x 8 is 2000" in log[-1]["messages"][-1]["content"]       # Echo rewrote it himself, with the note
    assert log[-1]["messages"][0]["content"] == "I am Echo."


def test_final_check_clean_or_gap_only_keeps_answer():
    async def checker(task, answer):
        return {"issues": [{"part": 0, "draft": "answer", "kind": "gap", "note": "could say more"}], "summary": "ok"}
    res = asyncio.run(daydream.run("t", local([]), daydream.DEFAULTS, checker=checker))
    assert not res.revised and res.final_issues == [] and res.helpers["checker"] == "used"


def test_socket_answer_tag_becomes_face(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)

    async def fake_run(task, loc, cfg, *a, **k):
        return daydream.Result(task=task, answer="[smiling] Yes, that works.", ok=True, parts=[task])
    monkeypatch.setattr(daydream, "run", fake_run)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream: x"})
        until(ws, "reply")
        r = until(ws, "reply")
        assert r["text"].startswith("Yes, that works.") and r["expression"] == "smiling"


# ---- rev e: stage events for the Dream Observatory, tower drafts through the bridge ----
def test_events_in_order_without_text():
    seen = []

    async def on_event(res, stage):
        seen.append(daydream.activity(res, stage, len(seen) + 1))

    async def checker(task, answer):
        return {"issues": [{"part": 0, "draft": "answer", "kind": "invented", "note": "n"}], "summary": ""}

    async def critic(task, parts, drafts):
        return {"issues": [{"part": 1, "draft": "echo", "kind": "gap", "note": "g"}], "summary": ""}
    asyncio.run(daydream.run("SECRET TASK", local([]), daydream.DEFAULTS, critic=critic, checker=checker,
                             on_event=on_event))
    assert [e["stage"] for e in seen] == ["enter", "plan", "drafts", "critique", "merge", "check", "done"]
    assert [e["seq"] for e in seen] == list(range(1, 8)) and seen[-1]["active"] is False and seen[0]["active"]
    assert seen[-1]["issues"] == {"mistake": 0, "gap": 1, "invented": 1} and seen[-1]["revised"] is True
    assert seen[-1]["elapsed_ms"] == seen[-1]["ms"]["total"] and len({e["daydream_id"] for e in seen}) == 1
    assert "SECRET" not in json.dumps(seen) and "Here's what I think" not in json.dumps(seen)


def test_event_failure_never_stops_daydream():
    async def broken(res, stage):
        raise RuntimeError("bridge down")
    assert asyncio.run(daydream.run("t", local([]), daydream.DEFAULTS, on_event=broken)).ok


UP = {"paired": True, "tower": {"on_network": True}, "link": {"ok": True}}
OFF = {"paired": True, "tower": {"on_network": False}, "link": {"ok": False}}


def test_tower_drafter_through_bridge():
    calls = []

    def handler(req):
        if req.url.path == "/status":
            return httpx.Response(200, json=UP)
        calls.append(json.loads(req.content))
        assert req.url.path == "/daydream/request"
        return httpx.Response(200, json={"status": "ok", "drafts": ["t1", None]})
    d = daydream.tower_drafter(daydream.DEFAULTS, transport=httpx.MockTransport(handler))
    res = asyncio.run(daydream.run("t", local([]), daydream.DEFAULTS, {"tower": d}))
    assert res.helpers["tower"] == "used" and res.drafts["tower"] == ["t1", None] and res.drafts["echo"][1]
    assert calls[0]["daydream_id"] == res.id and calls[0]["deadline_s"] == 90


def test_tower_busy_or_bridge_down():
    def busy(req):
        if req.url.path == "/status":
            return httpx.Response(200, json=UP)
        return httpx.Response(200, json={"status": "busy", "drafts": []})

    def down(req):
        raise httpx.ConnectError("no bridge")
    for h in (busy, down):
        d = daydream.tower_drafter(daydream.DEFAULTS, transport=httpx.MockTransport(h))
        res = asyncio.run(daydream.run("t", local([]), daydream.DEFAULTS, {"tower": d}))
        assert res.ok and res.helpers["tower"] in ("unavailable", "failed") and res.drafts["echo"] == ["draft for A", "draft for B"]


# ---- the owner 11-04: tower off → wake it like the 18:00 schedule does, wait, then draft ----
def bridge_sim(boot_after_polls=None, paired=True):
    """A fake bridge: off until `boot_after_polls` status checks after a wake (None = never boots)."""
    st = {"wakes": 0, "polls_after_wake": 0}

    def handler(req):
        if req.url.path == "/wake":
            st["wakes"] += 1
            return httpx.Response(200, json={"sent": True})
        if req.url.path == "/status":
            if st["wakes"]:
                st["polls_after_wake"] += 1
            on = boot_after_polls is not None and st["wakes"] and st["polls_after_wake"] > boot_after_polls
            return httpx.Response(200, json=(UP if on else OFF) | {"paired": paired})
        return httpx.Response(200, json={"status": "ok", "drafts": ["tower part"]})
    return st, httpx.MockTransport(handler)


def test_wake_when_off_then_draft(monkeypatch):
    monkeypatch.setattr(daydream.asyncio, "sleep", _nosleep)
    st, tr = bridge_sim(boot_after_polls=2)
    out = asyncio.run(daydream.tower_drafter(daydream.DEFAULTS, transport=tr)("t", ["p"], "id"))
    assert st["wakes"] == 1 and out == ["tower part"]


def test_never_comes_up_gives_up(monkeypatch):
    monkeypatch.setattr(daydream.asyncio, "sleep", _nosleep)
    t = iter(range(0, 10_000, 30))
    monkeypatch.setattr(daydream.time, "time", lambda: next(t))
    st, tr = bridge_sim(boot_after_polls=None)
    assert asyncio.run(daydream.wake_tower(150, tr)) is False and st["wakes"] == 1


def test_no_wake_when_up_or_unpaired_or_switched_off(monkeypatch):
    st, tr = bridge_sim(boot_after_polls=0, paired=False)
    assert asyncio.run(daydream.wake_tower(150, tr)) is False and st["wakes"] == 0
    cfg = {**daydream.DEFAULTS, "tower": {**daydream.DEFAULTS["tower"], "wake": False}}
    st, tr = bridge_sim(boot_after_polls=None)
    out = asyncio.run(daydream.tower_drafter(cfg, transport=tr)("t", ["p"], "id"))
    assert st["wakes"] == 0 and out == ["tower part"]      # wake off: ask anyway; the bridge says unavailable if down


async def _nosleep(_s):
    return None
