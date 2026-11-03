"""Stage 5a: knowledge gaps → proposed learning objectives; nothing acts until Alex decides."""
import time

from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory import gaps
from memory.store import MemoryStore


def test_detect():
    assert gaps.detect("memory", "unknown", "Sure, it was great.") == "personal"
    assert gaps.detect("memory", "known", "I don't remember that.") == "personal"
    assert gaps.detect("memory", "known", "Your sister is Ann.") is None
    assert gaps.detect("chat", "none", "I don't know the capital of that country.") == "general"
    assert gaps.detect("chat", "none", "Paris.") is None
    assert gaps.detect("command", "none", "I don't know") is None and gaps.detect(None, None, "no idea") is None


def test_repeats_merge_and_objectives(tmp_path):
    g = gaps.Gaps(tmp_path / "l.sqlite")
    a = g.note("personal", "What's Alex's favorite movie?", "loved_one")
    b = g.note("personal", "what is Alex's favorite movie", "owner")
    c = g.note("personal", "Where did Alex go to high school?", "loved_one")
    assert a == b != c
    obj = g.objectives()
    assert obj[0]["id"] == a and obj[0]["count"] == 2 and "Sam, Alex" in obj[0]["why"]
    assert obj[0]["suggested"] == "ask_owner"
    g.note("general", "What's the capital of Burkina Faso?", None)
    assert any(o["suggested"] == "research" and "Typed" in o["why"] for o in g.objectives())


def test_decisions_are_audited(tmp_path):
    g = gaps.Gaps(tmp_path / "l.sqlite")
    gid = g.note("personal", "Who are Alex's parents?", "loved_one")
    assert g.decide(gid, "ask_owner", "owner:panel", "ask them on a quiet evening")
    assert g.objectives() == [] and g.objectives("ask_owner")[0]["decided_by"] == "owner:panel"
    assert [h["what"] for h in g.history(gid)] == ["noticed", "ask_owner"]
    g.note("personal", "Who are Alex's parents?", "loved_one")       # decided gaps don't absorb new ones
    assert len(g.objectives()) == 1
    try:
        g.decide(gid, "delete everything")
        assert False
    except ValueError:
        pass


class Unsure(EchoBackend):
    async def chat_stream(self, messages):
        yield "[neutral] I don't remember that, will you tell me?"


def test_recorded_after_reply_and_api(tmp_path):
    client = TestClient(create_app(PersonaConfig(), Unsure(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Do you remember our trip to the lake?"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    obj = client.get("/api/learning/objectives").json()["objectives"]
    assert len(obj) == 1 and obj[0]["kind"] == "personal"
    assert client.post(f"/api/learning/objectives/{obj[0]['id']}", json={"decision": "nope"}).status_code == 400
    r = client.post(f"/api/learning/objectives/{obj[0]['id']}", json={"decision": "dismiss"}).json()
    assert r["ok"] and r["history"][-1]["what"] == "dismiss"
    assert client.get("/api/learning/objectives").json()["objectives"] == []


def test_ask_text_is_honest():
    assert gaps.ask_text("Where did Alex grow up?", "loved_one") == \
        'Sam asked me "Where did Alex grow up?" and I didn\'t know the answer. Will you tell me about it?'
    assert gaps.ask_text("who is tom", "owner,typed").startswith('I was asked "who is tom?"')


def test_approved_questions_go_first_in_curiosity(tmp_path):
    from memory.curiosity import Curiosity
    g = gaps.Gaps(tmp_path / "l.sqlite")
    a = g.note("personal", "Where did Alex grow up?", "loved_one")
    g.note("personal", "Who is Tom?", "loved_one")                      # not approved: never asked
    store = MemoryStore(tmp_path / "m.sqlite")
    c = Curiosity(store, extra=g.approved_questions)
    assert c.next_question()["id"] != f"gap-{a}"                   # nothing approved yet → the usual bank
    g.decide(a, "ask_owner")
    q = c.next_question()
    assert q["id"] == f"gap-{a}" and "Sam asked me" in q["text"] and gaps.gap_id(q) == a
    c.asked(q)
    assert c.next_question()["id"] != q["id"]                      # asked once, not again


def test_answer_marks_objective_done(tmp_path, monkeypatch):
    import memory.curiosity as mcu
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    g = gaps.Gaps(tmp_path / "learning.sqlite")
    gid = g.note("personal", "Where did Alex grow up?", "loved_one")
    assert client.post(f"/api/learning/objectives/{gid}", json={"decision": "ask_owner"}).json()["ok"]
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        for text in ("ask me something", "I grew up in a little town by the river, we went fishing every summer"):
            ws.send_json({"text": text})
            while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
                pass
    hist = [h["what"] for h in g.history(gid)]
    assert g.objectives("done") and "done" in hist


def test_research_needs_a_source(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    gid = gaps.Gaps(tmp_path / "learning.sqlite").note("general", "What is a rotary engine?", "owner")
    assert client.post(f"/api/learning/objectives/{gid}", json={"decision": "research"}).status_code == 400
    assert client.post(f"/api/learning/objectives/{gid}", json={"decision": "research", "url": "not a url"}).status_code == 400
    assert gaps.Gaps(tmp_path / "learning.sqlite").objectives()[0]["status"] == "open"   # nothing changed


def test_check_learning_rules(tmp_path):
    g = gaps.Gaps(tmp_path / "l.sqlite")
    a = g.note("personal", "Where did Alex grow up?", "loved_one")
    b = g.note("personal", "Who are Alex's parents?", "loved_one")
    c = g.note("personal", "What's Alex's favorite movie?", "loved_one")
    g.decide(a, "done", "owner:answered")
    g.decide(b, "done", "owner:answered")
    t = g.objectives("done")[0]["decided_at"]
    answers = {"Where did Alex grow up?": ("inferred", [(2, t - 999, "inferred"), (7, t + 5, "known")]),  # new one 2nd
               "Who are Alex's parents?": ("known", [(3, t - 999, "known")])}   # only an OLD memory: not this learning
    out = {r["id"]: r for r in gaps.check_learning(g, lambda q: answers[q], now=t + 10)}
    assert out[a]["outcome"] == "helped" and out[a]["detail"] == "known, memory 7"
    assert out[b]["outcome"] == "not_yet" and c not in out   # open objectives aren't checked
    assert gaps.check_learning(g, lambda q: answers[q], now=t + 20) == []                       # not again within 10 min
    later = gaps.check_learning(g, lambda q: ("unknown", []), now=t + gaps.GIVE_UP_S + 1)
    assert [r["outcome"] for r in later] == ["not_helped"]                                      # helped ones stay helped
    st = gaps.learning_stats(g)
    assert st["outcome_helped"] == 1 and st["outcome_not_helped"] == 1 and st["noticed"] == 3
    assert [h["what"] for h in g.history(b)].count("check: not_yet") == 1                       # repeats not logged


def test_check_with_real_retrieval(tmp_path):
    """Echo's own retrieval + grounding on a temporary memory: not helped → Alex's answer approved → helped."""
    from core import controller
    from memory import grounding
    from memory.entities import Entities
    from memory.retrieve import Retriever
    from memory.store import Memory
    store = MemoryStore(tmp_path / "m.sqlite")
    ents = Entities(store)
    r = Retriever(store, ents, None)

    def ask(q):
        res = r.retrieve(q, None, None, k=3)
        lab = grounding.label(res, controller.classify(q).kind).label
        return lab, [(h.memory.id, h.memory.created, grounding.hit_label(h).label) for h in res.hits]
    g = gaps.Gaps(tmp_path / "l.sqlite")
    gid = g.note("personal", "What was Alex's first car?", "loved_one")
    g.decide(gid, "done", "owner:answered")
    t = time.time()
    assert gaps.check_learning(g, ask, now=t)[0]["outcome"] == "not_yet"
    store.add(Memory(content="Alex's first car was a 1972 Ford Falcon.", status="confirmed", confidence=0.95,
                     source="owner:chat"))
    assert gaps.check_learning(g, ask, now=t + gaps.CHECK_EVERY_S + 1)[0]["outcome"] == "helped"


def test_only_real_questions_become_gaps():
    """CA 2.0 Phase 9: all 4 open objectives on 11-03 were false (statements, garbled speech, a test, a hedge)."""
    from memory import gaps
    no = "I don't know."
    assert gaps.detect("chat", None, no, "Alex here. Your three poems are in your learned reading now.") is None
    assert gaps.detect("chat", None, no, "Kill create") is None
    assert gaps.detect("reasoning", None, no, "This is Alex, testing. How many photos of Milo do I have?") is None
    assert gaps.detect("reasoning", None, "Six photos mention him; the others might, but I don't know for sure.",
                       "How many photos of Milo do I have?") is None
    assert gaps.detect("memory", None, "I don't remember that.", "When is my birthday?") == "personal"
    assert gaps.detect("chat", None, "I'm not sure.", "Who is Pat Jones?") == "general"
    assert gaps.detect("memory", None, "I don't remember.") == "personal"     # old callers: unchanged
