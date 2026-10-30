"""Echo asks the owner about his life (Phase 5b)."""
import json

from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.curiosity import Curiosity
from memory.store import Memory, MemoryStore

QS = [{"id": "c1", "period": "childhood", "text": "What's your earliest memory?"},
      {"id": "c2", "period": "childhood", "text": "Tell me about your mom."},
      {"id": "t1", "period": "teen", "text": "What was your first car?"}]


def make(tmp_path, **kw):
    bank = tmp_path / "bank.json"; bank.write_text(json.dumps(QS))
    store = MemoryStore(tmp_path / "m.sqlite")
    return store, Curiosity(store, bank=bank, **({"every_minutes": 30, "min_turns": 2} | kw))


def test_only_owners_voice_and_not_too_often(tmp_path):
    _, c = make(tmp_path)
    t = 1_000_000.0
    assert c.maybe_ask("loved_one", "hi", t) is None and c.maybe_ask("unknown", "hi", t) is None and c.maybe_ask(None, "hi", t) is None
    assert c.maybe_ask("owner", "hi", t) is None            # 1 turn: too soon
    q = c.maybe_ask("owner", "how are you", t)
    assert q is not None
    c.asked(q, t)
    assert c.maybe_ask("owner", "x", t + 60) is None          # waiting for the answer
    assert c.take_answer("owner", "Well, it was a long time ago...", t + 60)["id"] == q["id"]
    for i in range(5):
        assert c.maybe_ask("owner", "more", t + 120 + i) is None   # < 30 minutes since the last question
    assert c.maybe_ask("owner", "more", t + 31 * 60) is not None


def test_ask_me_something_forces_and_never_repeats(tmp_path):
    _, c = make(tmp_path, min_turns=99)
    seen = set()
    for i in range(3):
        q = c.maybe_ask("owner", "Echo, ask me a question", 1000.0 + i)
        assert q and q["id"] not in seen
        seen.add(q["id"]); c.asked(q, 1000.0 + i); c.take_answer("owner", "something", 1000.0 + i)
    assert c.maybe_ask("owner", "ask me something", 2000.0) is None     # bank used up
    assert c.maybe_ask("loved_one", "ask me something", 2000.0) is None
    _, typed = make(tmp_path / "t") if (tmp_path / "t").mkdir() is None else (None, None)
    assert typed.maybe_ask(None, "ask me a question", 1.0) and typed.maybe_ask("unknown", "ask me a question", 1.0) is None


def test_picks_the_time_of_life_known_least_and_skips_phone_answers(tmp_path):
    store, c = make(tmp_path)
    store.save(Memory(kind="memory", content="I rode my bike to the creek.", period="childhood"))
    assert c.next_question()["id"] == "t1"                              # no teen memories yet
    _, c2 = make(tmp_path / "b", answered={"t1", "c1"}) if (tmp_path / "b").mkdir() is None else (None, None)
    assert c2.next_question()["id"] == "c2"


def test_answers_only_from_owner_or_typed_within_window_and_declines(tmp_path):
    _, c = make(tmp_path)
    q = QS[0]
    c.asked(q, 100.0)
    assert c.take_answer("loved_one", "My earliest memory is...", 110.0) is None and c.pending(110.0)   # stays open
    assert c.take_answer("owner", "Not now, maybe later", 120.0) is None and not c.pending(120.0)  # declined: closed
    c.asked(q, 200.0)
    assert c.take_answer(None, "I remember my grandma's porch.", 200.0 + 601) is None             # too late
    c.asked(q, 300.0)
    assert c.take_answer(None, "I remember my grandma's porch.", 310.0)["id"] == q["id"]


def test_state_survives_restart(tmp_path):
    store, c = make(tmp_path)
    c.asked(QS[0], 100.0); c.queue_answer(QS[0], "My grandma's porch in Springfield.", 100.0)
    store.close()
    store2, c2 = make(tmp_path)
    assert c2.pending(150.0)["id"] == "c1" and c2.pop_answers()[0]["text"].startswith("My grandma") and c2.pop_answers() == []


class Capture(EchoBackend):
    def __init__(self, reply="Sure. What's your earliest memory?"):
        self.seen, self.reply = [], reply

    async def chat_stream(self, messages):
        self.seen.append(messages)
        for w in self.reply.split(" "):
            yield w + " "


def test_conversation_asks_owner_and_queues_his_answer(tmp_path, monkeypatch):
    import memory.curiosity as mcu
    bank = tmp_path / "bank.json"; bank.write_text(json.dumps(QS[:1]))
    monkeypatch.setattr(mcu, "BANK", bank)
    monkeypatch.setattr(mcu.Curiosity.__init__, "__defaults__", (True, 30, 4, bank, None))
    store = MemoryStore(tmp_path / "m.sqlite")
    b = Capture()
    with TestClient(create_app(PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), dataset_root=str(tmp_path / "none")),
                               b, store)).websocket_connect("/ws") as ws:
        ws.receive_json()
        def say(text, speaker):
            ws.send_json({"text": text, "speaker": speaker})
            while ws.receive_json().get("state") != "idle":
                pass
        say("Ask me something about my life", "loved_one")        # loved one: never asked (speaker filtered: no profiles → None)
        say("Ask me something about my life", "owner")
    assert all("learn more about the owner" not in m[-1]["content"] for m in b.seen)   # "owner" isn't a known profile here


def test_conversation_with_known_owner_voice(tmp_path, monkeypatch):
    import numpy as np
    import memory.curiosity as mcu
    from tests.test_speaker import FakeSpeakerID, tone
    bank = tmp_path / "bank.json"; bank.write_text(json.dumps(QS[:1]))
    monkeypatch.setattr(mcu.Curiosity.__init__, "__defaults__", (True, 30, 4, bank, None))
    model = tmp_path / "f.onnx"; model.write_bytes(b"x")
    sid = FakeSpeakerID(model, tmp_path / "spk"); sid.enroll("owner", [tone(150)], ["a"]); sid.enroll("loved_one", [tone(300)], ["b"])
    store = MemoryStore(tmp_path / "m.sqlite")
    b = Capture()
    with TestClient(create_app(PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), dataset_root=str(tmp_path / "none")),
                               b, store, speakers=sid)).websocket_connect("/ws") as ws:
        ws.receive_json()
        def say(text, speaker):
            ws.send_json({"text": text, "speaker": speaker})
            while ws.receive_json().get("state") != "idle":
                pass
        say("Ask me something about my life", "loved_one")
        say("Ask me something about my life", "owner")
        say("My earliest memory is sitting on my grandma's porch in Springfield eating watermelon.", "owner")
    users = [m[-1]["content"] for m in b.seen]
    assert "learn more about the owner" not in users[0] and "What's your earliest memory?" in users[1]
    assert "learn more about the owner" not in users[2]
    st = json.loads(store.get_meta("curiosity"))
    assert st["answers"][0]["question"] == "What's your earliest memory?" and "porch" in st["answers"][0]["text"]
    assert all("learn more" not in t["content"] for t in store.recent_turns(20))   # hint never stored


def test_asked_in_needs_the_real_question():
    from memory.curiosity import asked_in
    q = {"text": "What was your first car, and where did you go in it?"}
    assert asked_in("Oh nice. I've been wondering, what was the first car you ever had, and where'd you go in it?", q)
    assert not asked_in("Maybe a movie night instead?", q)
    assert not asked_in("I love cars.", q)
    assert asked_in("You're handy! Tell me about a trip or adventure from when you were young.",
                    {"text": "Tell me about a trip or adventure from when you were young."})   # a request, no '?'


def test_asked_in_rejects_a_different_question_sharing_one_word():
    from memory.curiosity import asked_in
    q = {"text": "What was your first car, and where did you go in it?"}
    assert not asked_in("What was it like when you first started dating Sam?", q)
