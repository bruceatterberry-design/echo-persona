"""CA 2.0 Phase 7: executive control — follow-up decision, module switches with reasons, the model-cache keeper."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from core import executive
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


# ---- memory retrieval: follow-ups ----
@pytest.mark.parametrize("text", [
    "What season does it look like it was taken in?",       # Test B (Phase 3): must stay a follow-up
    "Which of those two was at night?",
    "Is that Milo in it?",
    "Who took them?",
    "This is Alex. Where was that taken?",                 # an introduction first doesn't hide the follow-up
    "It's cold in that photo, isn't it?",                   # weather words, but "that photo" points back
])
def test_real_follow_ups_still_use_the_previous_message(text):
    plan, query = executive.plan_turn(text, "When was the dam photo taken?")
    assert plan.query == "with_previous" and query.startswith("When was the dam photo taken?")


@pytest.mark.parametrize("text,why", [
    ("This is Alex, testing. It's freezing in here.", "weather_or_time_it"),   # 11-03: pulled in 3 bridge photos
    ("It's freezing in here.", "weather_or_time_it"),
    ("What time is it?", "weather_or_time_it"),
    ("How's it going?", "weather_or_time_it"),
    ("It is getting late.", "weather_or_time_it"),
    ("This is Alex.", "intro_only"),
    ("This is Alex, testing. How many photos of Milo do I have?", "stands_alone"),
    ("Tell me about our trip to the dam", "stands_alone"),
])
def test_not_follow_ups(text, why):
    plan, query = executive.plan_turn(text, "When did I take the photo of the lit-up bridge?")
    assert (plan.query, plan.query_why, query) == ("as_said", why, text)


def test_no_previous_message_is_said_so():
    plan, query = executive.plan_turn("Which of those was first?", None)
    assert (plan.query, plan.query_why, query) == ("as_said", "no_previous_message", "Which of those was first?")


# ---- module activation ----
def test_modules_record_why_they_did_or_did_not_run(monkeypatch):
    monkeypatch.setenv("ECHO_SOCIAL_NOTES", "0")
    p, _ = executive.plan_turn("Hello", None)
    assert p.may("grounding") and not p.may("social")
    p.fired("grounding")
    assert p.as_record()["modules"] == {"grounding": "fired", "social": "switched_off"}
    nd, _ = executive.plan_turn("open the garage", None, not_done=True)
    assert not nd.may("date") and nd.modules == {"date": "command_not_done"}


def test_record_has_rule_names_only():
    p, _ = executive.plan_turn("Is that Milo in it?", "My secret dentist appointment", "memory", ["memory_words"])
    blob = json.dumps(p.as_record())
    assert "Milo" not in blob and "dentist" not in blob


# ---- the cache keeper ----
def test_cache_state_at_turn_start():
    k, now = executive.CacheKeeper(), 10_000.0
    assert k.state(None, now - 30, now) == "fresh"
    assert k.state({"by": "facts", "at": now}, now - 30, now) == "disturbed:facts"
    assert k.state(None, now - 3600, now) == "break"


def test_any_model_request_outside_the_conversation_marks_the_cache(monkeypatch):
    import httpx

    import core.sched as sched
    from core import llm
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("R", (), {"status_code": 200})())
    assert sched.SCHED.cache_disturbed is None
    llm._post("http://x/api/chat", {}, 1)                                   # e.g. Sam's writing (not a job)
    assert sched.SCHED.cache_disturbed["by"] == "other"
    assert sched.SCHED.cache_restored()["by"] == "other" and sched.SCHED.cache_disturbed is None
    sched.SCHED.cache_used("warm")                                          # the re-read itself doesn't count
    assert sched.SCHED.cache_disturbed is None


# ---- wired into Echo ----
class WarmCounter(EchoBackend):
    def __init__(self):
        super().__init__()
        self.warms = []

    async def warm(self, messages):
        self.warms.append(messages)


def chat(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass


def last_record(tmp_path):
    return json.loads(sqlite3.connect(tmp_path / "workspace.sqlite").execute(
        "SELECT record FROM turns ORDER BY id DESC LIMIT 1").fetchone()[0])


def test_turn_records_the_plan_and_the_cache_state(tmp_path):
    import core.sched as sched
    store = MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    chat(client, "When was the dam photo taken?")
    sched.SCHED.cache_used("facts")                    # a background job used the model in between
    chat(client, "What season does it look like it was taken in?")
    e = last_record(tmp_path)["exec"]
    assert e["query"] == "with_previous" and e["cache"] == "disturbed:facts"
    assert e["modules"].get("grounding") in ("fired", "quiet")
    assert sched.SCHED.cache_disturbed is None         # the reply read the conversation: cached again
    chat(client, "It's freezing in here.")
    e = last_record(tmp_path)["exec"]
    assert (e["query"], e["query_why"], e["cache"]) == ("as_said", "weather_or_time_it", "fresh")
    assert e["modules"]["social"] == "fired"
    r = client.get("/api/executive").json()
    assert r["turns"] == 3 and r["memory_search"]["with_previous:points_back"] == 1
    assert "ndas" in r["retired"]


def test_warm_prompt_is_exactly_the_beginning_of_the_next_one(tmp_path):
    """The re-read is only useful if the next real prompt starts with it: same instructions, same conversation."""
    store = MemoryStore(tmp_path / "m.sqlite")
    be = EchoBackend()
    seen = {}
    real_stream = be.chat_stream

    def spy(messages):
        seen["msgs"] = messages
        return real_stream(messages)
    be.chat_stream = spy
    app = create_app(PersonaConfig(), be, store)
    client = TestClient(app)
    chat(client, "Hello there.")
    chat(client, "Tell me about the dam.")
    warm = app.state.build_messages("Hello.", warm_only=True)
    chat(client, "And the bridge?")
    nxt = seen["msgs"]
    assert len(nxt) == len(warm) and warm[:-1] == nxt[:-1] and warm[-1]["content"] == "Hello."


def test_reread_waits_for_quiet_and_is_counted(tmp_path, monkeypatch):
    """The keeper's re-read runs as the scheduler's "warm" job and clears the mark."""
    import asyncio

    import core.sched as sched
    store = MemoryStore(tmp_path / "m.sqlite")
    be = WarmCounter()
    app = create_app(PersonaConfig(), be, store)
    client = TestClient(app)
    chat(client, "Hello there.")
    sched.SCHED.cache_used("facts")
    with pytest.raises(sched.Preempted):              # Echo is still speaking: conversation first, no re-read
        asyncio.run(app.state.reread("changed"))
    assert be.warms == [] and sched.SCHED.cache_disturbed is not None
    sched.SCHED.busy_until = time.time() - 60          # quiet now
    asyncio.run(app.state.reread("changed"))
    assert len(be.warms) == 1 and sched.SCHED.cache_disturbed is None
    assert be.warms[0][-1] == {"role": "user", "content": "Hello."}
    assert client.get("/api/executive").json()["cache_keeper"]["reads"] == {"changed": 1}


def test_keeper_rereads_once_after_a_break():
    k, now = executive.CacheKeeper(), 10_000.0
    assert k.due(7, now - 1000, now) is None
    assert k.due(7, now - executive.BREAK_S - 1, now) == "after_break"
    k.done("after_break", 4000, 7)
    assert k.due(7, now - executive.BREAK_S - 1, now) is None              # once per break
    assert k.due(8, now - executive.BREAK_S - 1, now) == "after_break"     # a new conversation, a new break


def test_exact_prefix_matches_ollamas_qwen35_rendering():
    """Rendering checked against the real model 11-03 (same prompt token count, next reply 2.5 s → 0.25 s)."""
    from core.config import LLMConfig
    from core.llm import OllamaBackend
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}, {"role": "assistant", "content": "A"}]
    be = OllamaBackend(LLMConfig(model="qwen3.5:9b-q4_K_M", think=False))
    assert be.exact_prefix(msgs) == ("<|im_start|>system\nS<|im_end|>\n<|im_start|>user\nU<|im_end|>\n"
                                     "<|im_start|>assistant\nA<|im_end|>\n")
    assert OllamaBackend(LLMConfig(model="qwen3.5:9b-q4_K_M", think=True)).exact_prefix(msgs) is None
    assert OllamaBackend(LLMConfig(model="llama3", think=False)).exact_prefix(msgs) is None
    assert be.exact_prefix([*msgs, {"role": "tool", "content": "x"}]) is None


def test_warm_reads_only_what_comes_before_the_stand_in_message():
    import asyncio

    from core.config import LLMConfig
    from core.llm import OllamaBackend
    sent = {}

    class C:
        async def post(self, url, json):
            sent.update(url=url, body=json)
    be = OllamaBackend(LLMConfig(model="qwen3.5:9b-q4_K_M", think=False))
    be.client = C()
    asyncio.run(be.warm([{"role": "system", "content": "S"}, {"role": "user", "content": "Hello."}]))
    assert sent["url"] == "/api/generate" and sent["body"]["raw"] is True
    assert sent["body"]["prompt"] == "<|im_start|>system\nS<|im_end|>\n" and "Hello." not in sent["body"]["prompt"]
    assert sent["body"]["options"]["num_predict"] == 1


# ---- Phase 8: Alex's lessons in action ----
@pytest.mark.parametrize("step", [1, 2, 3, 5, 8, 50])
@pytest.mark.parametrize("reply,want", [
    ("[smiling] That sounds wonderful, Alex. It is always good to share a nice dinner with Sam.",
     "It is always good to share a nice dinner with Sam."),
    ("That is wonderful news, Alex! Seeing those purple clematis climb must look beautiful.",
     "Seeing those purple clematis climb must look beautiful."),
    ("That's wonderful to hear, Alex; it sounds like a good day.", "It sounds like a good day."),
    ("That sounds like a wonderful way to spend your childhood, Alex. Fixing cars taught you a lot.",   # content: kept
     "That sounds like a wonderful way to spend your childhood, Alex. Fixing cars taught you a lot."),
    ("That is wonderful news about the trellis; it looks great.", "That is wonderful news about the trellis; it looks great."),
    ("That sounds wonderful, Alex.", "That sounds wonderful, Alex."),                       # nothing else: kept
    ("Thanks, Alex. I'm glad.", "Thanks, Alex. I'm glad."),
    ("It's raining here.", "It's raining here."),
])
def test_stock_opener_is_dropped_only_when_it_is_nothing_else(reply, want, step):
    from core.schema import StreamParser
    p = StreamParser(trim_opener=True)
    shown = "".join(p.feed(reply[i:i + step]) for i in range(0, len(reply), step))
    r = p.finish()
    assert r.text == want and (shown + p.leftover).strip() == want


def test_opener_filter_is_off_unless_asked():
    from core.schema import StreamParser
    p = StreamParser()
    p.feed("That sounds wonderful, Alex. It was fun.")
    assert p.finish().text == "That sounds wonderful, Alex. It was fun."


def test_style_reminder_uses_the_owners_own_lessons_for_statements_only():
    from core import development as dev
    lessons = [{"id": 2, "text": "I vary how I begin my replies.", "active": True},
               {"id": 4, "text": "I don't end every reply with a question. I ask one only when I really want to know.",
                "active": True},
               {"id": 7, "text": "I talk like a person: no narrating or summarizing what they just said.", "active": True},
               {"id": 9, "text": "I don't end every reply with a question (old).", "active": False}]
    n = dev.reminder("Sam and I had a really nice dinner tonight.", lessons)
    assert n.startswith("[Style note:") and "end every reply with a question. I ask" in n and "narrating" in n
    assert "vary how I begin" not in n and "(old)" not in n          # openings: the filter; inactive: never
    assert "end every reply" not in dev.reminder("I fixed the trellis.", lessons, asking_hint=True)
    for q in ("What did we do last weekend?", "How are you", "okay", "open YouTube", ""):
        assert dev.reminder(q, lessons) == ""


def test_lesson_report_before_and_since(tmp_path):
    from core import development as dev
    t0 = time.mktime(time.strptime("2026-11-02 07:19", "%Y-%m-%d %H:%M"))
    turns = []
    for i in range(30):   # before: no question endings; since: all
        turns += [{"role": "user", "content": "I fixed it.", "ts": t0 - 86400 + i},
                  {"role": "assistant", "content": f"Nice work number {i}.", "ts": t0 - 86400 + i}]
    for i in range(30):
        turns += [{"role": "user", "content": "I fixed it.", "ts": t0 + 60 + i},
                  {"role": "assistant", "content": f"How did you do {i}?", "ts": t0 + 60 + i}]
    turns += [{"role": "user", "content": "This is Alex, testing.", "ts": t0 + 999},
              {"role": "assistant", "content": "Okay?", "ts": t0 + 999}]          # test turns don't count
    les = [{"id": 4, "text": "I don't end every reply with a question.", "added": "2026-11-02 07:19", "active": True},
           {"id": 1, "text": "Poems: title and author first.", "added": "2026-11-01 22:01", "active": True}]
    r = dev.report(les, turns)
    q = r["lessons"][0]["measured"][0]
    assert (q["before"]["missed"], q["since"]["missed"], q["since"]["turns"]) == (0, 30, 30)
    assert r["lessons"][0]["verdict"] == "worse_since" and r["lessons"][1]["verdict"] == "not_measured_automatically"
    assert "fixed" not in json.dumps(r)                                      # rates and counts only


def test_layers_and_development_endpoints(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    (tmp_path / "lessons.json").write_text(json.dumps({"lessons": [
        {"id": 1, "text": "I don't end every reply with a question.", "added": "2026-11-02 07:19", "active": True}]}))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    chat(client, "I fixed the trellis.")
    d = client.get("/api/development").json()
    assert d["lessons"][0]["measured"][0]["check"] == "question_ending"
    lay = client.get("/api/personality/layers").json()
    assert set(lay) == {"foundation", "stable_tendencies", "learned_preferences", "temporary_state", "history"}
    assert lay["learned_preferences"][0]["id"] == 1
    assert any(h["what"] == "lesson added" for h in lay["history"])
    rec = last_record(tmp_path)
    assert rec["exec"]["modules"].get("style") in ("fired", "quiet") and "style_filter" in rec["exec"]["modules"]
