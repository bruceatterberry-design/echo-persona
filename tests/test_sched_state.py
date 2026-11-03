"""CA 2.0 Phase 2: conversation-first scheduler (core/sched.py) and persistent cognitive state (core/state.py)."""
import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import core.sched as sched
from core.sched import Preempted, Scheduler
from core.state import CognitiveState


def mk(tmp_path, free=50, **settings):
    p = tmp_path / "sched.json"
    if settings:
        p.write_text(json.dumps(settings))
    return Scheduler(settings=p, memory_probe=lambda: free)


def test_waits_for_conversation_then_runs(tmp_path):
    s = mk(tmp_path, quiet_seconds=0.2)
    with s.job("facts"):
        pass
    s.turn_started()
    with pytest.raises(Preempted, match="conversation"):
        with s.job("facts"):
            pass
    s.spoken_until(time.time() + 0.1)          # reply spoken for 0.1 s more, then 0.2 s quiet
    assert s.why_not() == "conversation"
    time.sleep(0.35)
    assert s.quiet()
    j = s.snapshot()["jobs"]["facts"]
    assert (j["runs"], j["done"], j["waited"]) == (1, 1, 1)


def test_turn_never_reported_back_expires(tmp_path, monkeypatch):
    s = mk(tmp_path, quiet_seconds=0)
    monkeypatch.setattr(sched, "TURN_MAX_S", 0.1)
    s.turn_started()
    assert not s.quiet()
    time.sleep(0.15)
    assert s.quiet()


def test_memory_pressure_and_pause(tmp_path):
    assert mk(tmp_path, free=8).why_not() == "memory low (8% free)"
    assert mk(tmp_path, free=None).quiet()      # no reading: never blocks
    s = mk(tmp_path)
    s.manual_pause = True
    assert s.why_not() == "paused"
    s.manual_pause = None
    assert mk(tmp_path, background=False).why_not() == "paused"


def test_preempted_passes_through_catch_all(tmp_path):
    """Jobs that catch every Exception (and mark items failed) must not swallow an interruption."""
    s = mk(tmp_path)
    s.turn_started()
    caught = []
    def job_that_catches_everything():
        try:
            with s.job("learn"):
                pass
        except Exception:
            caught.append("marked failed")
    with pytest.raises(Preempted):
        job_that_catches_everything()
    assert caught == []


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop); t.join(2)


def test_talking_cancels_the_request_in_flight(tmp_path, loop_thread):
    s = mk(tmp_path)
    s.attach(loop_thread)
    started, result = threading.Event(), {}
    async def slow_llm():
        started.set()
        await asyncio.sleep(10)
        return "finished"
    def worker():
        t = time.perf_counter()
        try:
            with s.job("summaries"):
                result["r"] = s.call(slow_llm)
        except Preempted as e:
            result["preempted"] = str(e)
        result["seconds"] = time.perf_counter() - t
    w = threading.Thread(target=worker); w.start()
    assert started.wait(2)
    assert s.snapshot()["running"] == ["summaries"]
    assert s.turn_started() == 1
    w.join(3)
    assert result.get("preempted") == "conversation" and result["seconds"] < 1
    snap = s.snapshot()
    assert snap["preemptions"] == 1 and snap["jobs"]["summaries"]["preempted"] == 1 and snap["running"] == []


def test_async_background_work_is_cancelled(tmp_path):
    async def main():
        s = mk(tmp_path)
        s.attach(asyncio.get_running_loop())
        async def warm():
            await asyncio.sleep(10)
        t = asyncio.create_task(s.run("warm", warm()))
        await asyncio.sleep(0.05)
        s.turn_started()
        with pytest.raises(Preempted):
            await t
        s.turn_started()
        with pytest.raises(Preempted):   # still talking: a new one doesn't start
            await s.run("warm", warm())
    asyncio.run(main())


def test_chat_post_outside_a_job_is_unchanged(monkeypatch):
    import core.llm as llm
    calls = []
    monkeypatch.setattr(llm.httpx, "post", lambda url, timeout, json: calls.append(url) or type("R", (), {"status_code": 200})())
    llm.chat_post("http://h", {"messages": [{"role": "user", "content": "x"}]}, 5)
    assert calls == ["http://h/api/chat"]


def test_curiosity_answers_requeued(tmp_path):
    from memory.curiosity import Curiosity
    from memory.store import MemoryStore
    c = Curiosity(MemoryStore(tmp_path / "m.sqlite"))
    c.queue_answer({"text": "Q1"}, "A1"); c.queue_answer({"text": "Q2"}, "A2")
    got = c.pop_answers()
    c.requeue(got[1:])
    assert [a["text"] for a in c.pop_answers()] == ["A2"]


# ---- persistent state ----
def test_state_survives_restart_and_expires(tmp_path):
    from core.browser import Action
    st = CognitiveState(tmp_path / "state.json")
    assert st.restored_from is None
    st.turn("owner", "llm", "memory", [5, 3], ["Milo"])
    st.turn("loved_one", "llm", "chat", [7, 5])
    st.offer(Action(say="Opening the Weather Channel.", url="https://weather.com"))
    st.set("opened", {"host": "weather.com", "name": "the Weather Channel", "at": time.time() - 4000})   # too old
    st2 = CognitiveState(tmp_path / "state.json")
    assert st2.restored_from == "state.json"
    assert st2.get("last_turn")["speaker"] == "loved_one"
    assert st2.get("focus")["memory_ids"] == [7, 5, 3] and st2.get("focus")["entities"] == ["Milo"]
    act, at = st2.restored_offer(Action)
    assert act.url == "https://weather.com" and act.say.startswith("Opening")
    assert st2.get("opened") is None                                  # stale items are not restored
    assert oct((tmp_path / "state.json").stat().st_mode)[-3:] == "600"
    st2.data["offer"]["at"] = time.time() - 300
    assert st2.restored_offer(Action) is None


def test_state_never_stores_scripts_and_falls_back(tmp_path):
    from core.browser import Action
    st = CognitiveState(tmp_path / "state.json")
    st.offer(Action(say="Closing it.", script='tell application "Finder" to quit'))
    st.turn("owner", "llm", "chat", [1])
    assert "script" not in (tmp_path / "state.json").read_text()
    (tmp_path / "state.json").write_text("{damaged")
    st2 = CognitiveState(tmp_path / "state.json")
    assert st2.restored_from == "state.prev.json"                     # the last good state


def test_server_saves_state_and_restores_offer(tmp_path):
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    from memory.store import MemoryStore
    from voice.stt import FakeSTT
    store = MemoryStore(tmp_path / "m.sqlite")
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "offer": {"at": time.time() - 20, "act": {
        "say": "Opening the Weather Channel.", "url": "https://weather.com"}}}))
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), store, FakeSTT()))
    assert c.get("/api/state").json()["offer"]["fresh"]
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()                                             # history
        ws.send_json({"text": "yes please"})
        while (m := ws.receive_json())["type"] != "reply":
            pass
    assert "Weather" in m["text"]                                     # the offer made before the restart still works
    st = c.get("/api/state").json()
    assert "offer" not in st and st["last_turn"]["route"] and st["opened"]["host"] == "weather.com"
    s = c.get("/api/scheduler").json()
    assert s["reason"] == "conversation"                              # just talked: background waits
    assert c.post("/api/scheduler", json={"pause": True}).json()["reason"] == "paused"
    assert c.post("/api/scheduler", json={"pause": "x"}).status_code == 400


def test_startup_warmup_gives_way_to_a_reply(tmp_path):
    """Right after a restart Echo reads his prompt in advance; a message arriving meanwhile must not wait behind it."""
    import core.server as srv
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from memory.store import MemoryStore
    from voice.stt import FakeSTT
    state = {}
    class SlowWarm(EchoBackend):
        async def warm(self, messages):
            state["started"] = time.perf_counter()
            await asyncio.sleep(30)
            state["finished"] = True
    with TestClient(srv.create_app(PersonaConfig(), SlowWarm(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT())) as c:
        for _ in range(100):
            if "started" in state:
                break
            time.sleep(0.05)
        assert "started" in state and srv.SCHED.snapshot()["running"] == ["warm"]
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            t = time.perf_counter()
            ws.send_json({"text": "hello there"})
            while ws.receive_json()["type"] != "reply":
                pass
        assert time.perf_counter() - t < 5 and "finished" not in state
        assert srv.SCHED.snapshot()["jobs"]["warm"]["preempted"] == 1
