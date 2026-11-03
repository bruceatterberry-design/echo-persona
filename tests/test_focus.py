"""CA 2.0 Phase 3: carry-over after a break and topic-aware memory search (core/focus.py)."""
import time

from core import focus
from memory.store import MemoryStore


def test_referential_messages():
    yes = ["What season does it look like it was taken in?", "Which of those two was taken at night?",
           "And the other one?", "Tell me more about that"]
    no = ["Do you remember the photo of the dam?", "What flowers have I shared photos of?", "Good morning Echo"]
    assert all(map(focus.is_referential, yes)) and not any(map(focus.is_referential, no))
    long = "That reminds me of a really long story about " + "the old farm and the barn " * 4
    assert not focus.is_referential(long)                                   # long messages name their own subject
    assert focus.expand_query("What season was it?", "Do you remember the dam photo?").startswith("Do you remember")
    assert focus.expand_query("What flowers grow there in spring and summer near the road?", None) == \
        "What flowers grow there in spring and summer near the road?"
    assert focus.expand_query("Good morning", "x") == "Good morning"


def talk(store, pairs, ts):
    for u, a in pairs:
        store.add_turn("user", u, "owner"); store.add_turn("assistant", a)
    store.db.execute("UPDATE turns SET ts = ?", (ts,)); store.db.commit()


def test_carryover_after_a_break(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    now = time.time()
    talk(s, [("I want purple clematis on the back fence.", "Lovely."), ("First I must fix the leaning trellis.", "Good idea."),
             ("Cedar for the posts.", "Cedar lasts."), ("Aja came out in 1977.", "Yes."), ("Clean sound?", "Careful mixing."),
             ("Maybe tonight.", "Enjoy.")], now - 3 * 3600)
    assert focus.carryover(s) == ("", [])                                   # nothing has left the window yet
    s.turns_window()                                                        # back after the break: window keeps 8 turns
    carry, ids = focus.carryover(s, now)
    assert "fix the leaning trellis" in carry and "clematis" in carry and "Cedar" not in carry
    assert carry.startswith("Earlier in this conversation") and 'Alex said: "I want purple clematis' in carry
    assert len(ids) == 2
    s.set_meta("summarized_upto", str(max(ids) + 1))                        # a note now covers those turns
    assert focus.carryover(s, now) == ("", [])
    s.set_meta("summarized_upto", "0")
    assert focus.carryover(s, now + 2 * 86400) == ("", [])                  # too old to carry


def test_carryover_reaches_the_prompt_and_stays_stable(tmp_path):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    from voice.stt import FakeSTT
    s = MemoryStore(tmp_path / "m.sqlite")
    talk(s, [(f"Garden plan step {i}: fix the trellis" if i == 0 else f"Other topic {i}", "ok") for i in range(6)],
         time.time() - 3 * 3600)
    sent = []
    class Spy(EchoBackend):
        async def chat_stream(self, messages):
            sent.append(messages)
            async for x in super().chat_stream(messages):
                yield x
    c = TestClient(create_app(PersonaConfig(), Spy(), s, FakeSTT()))
    for msg in ("Hi, I'm back", "What was step one?"):
        with c.websocket_connect("/ws") as ws:
            ws.receive_json(); ws.send_json({"text": msg})
            while ws.receive_json()["type"] != "reply":
                pass
    assert len(sent) == 2 and "Garden plan step 0: fix the trellis" in sent[0][0]["content"]
    assert sent[0][0]["content"] == sent[1][0]["content"]                    # same system block: no extra re-reading
    st = c.get("/api/workspace/current").json()
    assert st["carryover"]["lines"] >= 1 and "state" in st and "scheduler" in st
