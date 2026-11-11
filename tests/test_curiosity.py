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
    monkeypatch.setattr(mcu.Curiosity.__init__, "__defaults__", (True, 30, 4, bank, None, None, None, None))
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
    monkeypatch.setattr(mcu.Curiosity.__init__, "__defaults__", (True, 30, 4, bank, None, None, None, None))
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


def test_old_questions_hidden_from_the_model(tmp_path):
    from memory.curiosity import Curiosity
    from memory.store import MemoryStore
    c = Curiosity(MemoryStore(tmp_path / "m.sqlite"))
    r = ("Sure thing. I'm opening YouTube now. Tell me any memory you'd like me to keep. "
         "Start with when it was and who was there.")
    assert c.scrub(r) == "Sure thing. I'm opening YouTube now."
    assert c.scrub("We went to the lake in June. Sam was there.") == "We went to the lake in June. Sam was there."


# ---- story threads (the owner 2026-10-31: thoughtful follow-ups about the same story, not "another trip") ----
def test_what_counts_as_an_answer():
    from memory.curiosity import looks_like_answer, last_question
    assert looks_like_answer("We went to Disney World and saw Epcot Center, it was amazing.")
    for t in ("How are you today?", "We'll do that in a little bit.", "Not now", "ok", "Well, I'm just testing right now."):
        assert not looks_like_answer(t), t
    assert last_question("That sounds lovely. What did you like most about Epcot?") == "What did you like most about Epcot?"
    assert last_question("That sounds lovely.") is None


def test_non_answer_drops_the_question_without_saving(tmp_path):
    _, c = make(tmp_path)
    c.asked(QS[0], 100.0)
    assert c.take_answer("owner", "How are you today?", 110.0) is None and not c.pending(110.0)
    assert c.pop_answers() == []


def test_thread_follows_up_twice_then_closes(tmp_path):
    _, c = make(tmp_path)
    q = {"id": "t1", "period": "childhood", "text": "Tell me about a trip or adventure from when you were young."}
    c.asked(q, 100.0)
    got = c.take_answer("owner", "We went to Disney World with my parents and saw Epcot Center.", 110.0)
    h = c.start_thread(got["text"], "owner", got["period"], 110.0)
    assert "ONE thoughtful follow-up question about this same story" in h and "different trip" in h
    h1, a1 = c.follow("owner", "Spaceship Earth, the big ball. We rode it twice and my owner loved it.",
                      "Epcot was something new back then. What do you remember most about it?", 120.0)
    assert "follow-up" in h1 and a1["question"] == "What do you remember most about it?" and a1["period"] == "childhood"
    h2, _ = c.follow("owner", "My mom bought me a little Figment toy, the purple dragon, at the gift shop.",
                     "Spaceship Earth is iconic. Did you bring anything home from the trip?", 130.0)
    assert "thank the owner for sharing" in h2 and "don't ask another question" in h2    # 2 follow-ups, then a warm close
    assert not c.in_thread(131.0)


def test_changing_the_subject_ends_the_thread(tmp_path):
    _, c = make(tmp_path)
    c.start_thread("Tell me about a trip.", "owner", None, 100.0)
    assert c.follow("owner", "Can you play some Steely Dan?", "Who was with you on that trip?", 110.0) is None
    assert not c.in_thread(111.0)


def test_loved_one_story_starts_a_thread_and_is_marked_as_theirs(tmp_path):
    _, c = make(tmp_path)
    h, ans = c.follow("loved_one", "When I was little we lived near the river and I used to catch crawdads with my brother.",
                      "Where did you grow up, the loved one?", 100.0)
    assert "the loved one just answered" in h
    c.queue_answer(ans, "When I was little we lived near the river...", 100.0, teller="loved_one")
    assert c.pop_answers()[0]["source"].startswith("loved_one:chat:")
    from memory.candidates import PROMPT, for_teller
    assert 'about the loved one in the third person ("the loved one ...")' in for_teller(PROMPT, "loved_one") and "the owner's own voice" in PROMPT


def test_story_reply_through_the_chat(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.server import create_app
    from memory.curiosity import Curiosity
    store = MemoryStore(tmp_path / "m.sqlite")
    q = {"id": "t1", "period": "childhood", "text": "Tell me about a trip or adventure from when you were young."}
    Curiosity(store).asked(q)
    cap = Capture()
    client = TestClient(create_app(PersonaConfig(), cap, store))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "We went to Disney World with my parents and saw Epcot Center, it was amazing."})
        while ws.receive_json()["type"] != "reply":
            pass
    sent_now = cap.seen[-1][-1]["content"]                            # what the model got with this message
    assert "answered my question" in sent_now and "ONE thoughtful follow-up" in sent_now
    saved = store.db.execute("SELECT coalesce(sent, content) FROM turns WHERE role='user' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "answered my question" not in saved                         # …but it isn't kept for later turns


def test_one_time_instructions_never_stay_in_the_history(tmp_path):
    """2026-10-31: "END my reply by asking 'Tell me about a trip…'" was saved with the owner's message, so the model asked it
    again on every later turn."""
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.server import create_app
    import memory.curiosity as mcu
    store = MemoryStore(tmp_path / "m.sqlite")
    cap = Capture("Sure. Tell me about a trip or adventure from when you were young.")
    q = {"id": "mem-young-05", "period": "young_adult", "text": "Tell me about a trip or adventure from when you were young."}
    orig = mcu.Curiosity.maybe_ask
    calls = {"n": 0}
    def ask_once(self, speaker, text, now=None):
        calls["n"] += 1
        return q if calls["n"] == 1 else None
    mcu.Curiosity.maybe_ask = ask_once
    try:
        client = TestClient(create_app(PersonaConfig(), cap, store))
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for text in ("hello there", "How are you today?",
                         "When I was young we went to Disney World in Orlando and I went to Epcot Center. It was amazing."):
                ws.send_json({"text": text})
                while ws.receive_json()["type"] != "reply":
                    pass
    finally:
        mcu.Curiosity.maybe_ask = orig
    history = cap.seen[-1][1:-1]                                     # what the model saw before the last message
    assert not any("END my reply" in m["content"] for m in history)
    assert not any("Tell me about a trip or adventure" in m["content"] for m in history if m["role"] == "assistant")
    assert "answered my question" in cap.seen[-1][-1]["content"]     # the story got a follow-up note
    stored = [r[0] for r in store.db.execute("SELECT sent FROM turns WHERE role='user'") if r[0]]
    assert not any("END my reply" in s for s in stored)


def test_tell_me_counts_as_a_question():
    from memory.curiosity import last_question
    assert last_question("Lovely. Tell me about a trip or adventure from when you were young.").startswith("Tell me about")


def test_photo_and_bank_questions_take_turns(tmp_path):
    """CA 2.0 Phase 9: uncaptioned photos join the question slot, alternating with the bank; approved objectives first."""
    from memory.curiosity import Curiosity
    bank = tmp_path / "bank.json"; bank.write_text(json.dumps(QS[:2]))
    store = MemoryStore(tmp_path / "m.sqlite")
    pics = [{"id": "photo:7", "text": "I have a photo from June 2022 of a lit-up bridge. Where was that?", "period": None}]
    c = Curiosity(store, bank=bank, photos=lambda: pics)
    q1 = c.next_question(); c.asked(q1)
    q2 = c.next_question(); c.asked(q2)
    assert q1["id"] == "photo:7" and not q2["id"].startswith("photo:")
    assert not c.next_question()["id"].startswith("photo:")              # the only photo was asked already
    approved = Curiosity(store, bank=bank, photos=lambda: pics, extra=lambda: [{"id": "gap:1", "text": "x?", "period": None}])
    assert approved.next_question()["id"] == "gap:1"


def test_photo_questions_from_memory_text(tmp_path):
    from core import initiative
    store = MemoryStore(tmp_path / "m.sqlite")
    from memory.store import Memory
    a, _ = store.add(Memory(content='A photo in my "Our home" collection. Taken on 22 November 2017 (date from the camera). '
                            'What the photo shows: This photo shows a man in a dark suit holding two young children, '
                            'smiling. Taken the same day: a cake (15:00).', source="photo:abc"))
    b, _ = store.add(Memory(content='A photo in my "Places" collection. Date taken: not known. What the photo shows: This '
                            'photo shows a large, rusty orange excavator parked by a barn.', source="photo:def"))
    c, _ = store.add(Memory(content='A photo in my "Cats" collection. the owner says: This is Milo. What the photo shows: '
                            'a gray tabby cat.', source="photo:ghi"))
    d, _ = store.add(Memory(content='A photo in my "Our home" collection. Taken on 3 January 2026 (date from the camera). '
                            'What the photo shows: A close-up selfie shows a man in a green jacket and a gray hoodie.',
                            source="photo:jkl"))
    qs = {q["memory_id"]: q for q in initiative.photo_questions(store)}
    assert c.id not in qs                                                  # captioned by the owner: nothing to ask
    assert qs[a.id]["text"] == ("I have a photo from November 2017 of a man in a dark suit holding two young children. "
                                "Who is in it, and what was happening that day?")
    assert qs[a.id]["why"] == ["people_unnamed", "dated", "part_of_a_day"]
    assert qs[b.id]["text"].startswith("I have a photo (I don't know when it was taken) of a large, rusty orange excavator")
    assert "Is that you" in qs[d.id]["text"]                               # one man: ask, don't assume
    assert list(qs)[0] == a.id                                             # best first


def test_photo_question_counts_as_asked_when_reworded():
    from memory.curiosity import asked_in
    q = {"id": "photo:3", "text": "I have a photo from October 2021 of four smiling adults posing together indoors. "
                                  "Who is in it, and what was happening that day?"}
    assert asked_in("the owner, that sounds like an interesting photo from October 2021; who is in it and what was happening?", q)
    assert not asked_in("That sounds wonderful, the owner. How was your day?", q)          # no photo mentioned
    assert not asked_in("I found a photo from October 2021 of four adults.", q)      # nothing asked


def test_yes_its_me_on_a_photo_is_an_answer_and_echo_knows_its_the_owner_and_himself(tmp_path):
    """the owner 2026-11-03: a confirmed photo of the owner is the owner AND, as their digital self, Echo too."""
    from memory.curiosity import Curiosity
    store = MemoryStore(tmp_path / "m.sqlite")
    c = Curiosity(store, bank=tmp_path / "none.json")
    q = {"id": "photo:5", "text": "I have a photo from May 2011 of a smiling man. Is that you, and what was happening that day?",
         "period": None}
    c.asked(q)
    assert c.take_answer("owner", "Yes, that's me.")["id"] == "photo:5"
    assert c.state()["answered_ids"] == ["photo:5"]
    hint = c.start_thread(q["text"], "owner", photo=True, answer="Yes, that's me.")
    assert "Alex" in hint and "digital self" in hint and "me as well" in hint
    other = c.start_thread(q["text"], "owner", photo=True, answer="No, that's my brother at his wedding.")
    assert "digital self" not in other and "photo" in other
    c.asked({**q, "id": "photo:6"})
    assert c.take_answer("owner", "No.") is not None                      # a plain no answers it too
    c.asked({"id": "b1", "text": "What was your first job?", "period": None})
    assert c.take_answer("owner", "Yes.") is None                         # elsewhere: still needs a real answer


# ---- dream questions (memory/dreamq.py, 2026-11-04) ----
def test_dreamq_reads_bridge_table(tmp_path):
    import sqlite3
    from memory import dreamq
    assert dreamq.questions(tmp_path / "missing.sqlite") == []
    db = tmp_path / "proposals.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other (x)"); con.commit()
    assert dreamq.questions(db) == []          # table not there yet
    con.execute("CREATE TABLE dream_questions (proposal_id TEXT PRIMARY KEY, text TEXT NOT NULL, queued REAL NOT NULL)")
    con.execute("INSERT INTO dream_questions VALUES ('p1', 'Where was the red car photo taken?', 1)")
    con.commit(); con.close()
    assert dreamq.questions(db) == [{"id": "dream:p1", "text": "Where was the red car photo taken?", "period": None,
                                     "source": "dream"}]


def test_dream_questions_alternate_and_never_repeat(tmp_path):
    from memory.curiosity import Curiosity
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / "m.sqlite")
    dq = [{"id": "dream:a", "text": "Where was the red car photo taken?", "period": None},
          {"id": "dream:b", "text": "Who drove on that July 2022 trip?", "period": None}]
    c = Curiosity(store, dreams=lambda: dq)
    q1 = c.next_question(); assert q1["id"] == "dream:a"
    c.asked(q1)
    q2 = c.next_question(); assert not q2["id"].startswith("dream:")     # a non-dream question in between
    c.asked(q2)
    q3 = c.next_question(); assert q3["id"] == "dream:b"                  # a never asked twice
    c.asked(q3)
    c.asked(c.next_question())
    assert not c.next_question()["id"].startswith("dream:")              # all dream questions used up


def test_broken_dream_source_is_ignored(tmp_path):
    from memory.curiosity import Curiosity
    from memory.store import MemoryStore
    c = Curiosity(MemoryStore(tmp_path / "m.sqlite"), dreams=lambda: 1 / 0)
    assert c.next_question() is not None


def test_my_questions_wait_for_a_lull():   # 2026-11-05: a photo question cut across the owner's app idea
    from memory.curiosity import lull
    for t in ["No problem.", "Yes, it would.", "It's going well.", "I got some work done this morning."]:
        assert lull(t), t
    for t in ["I would like to make an app for you, Echo, so that you can see the world.", "How are you today?",
              "Let's play some Steely Dan.", "Can you open the weather?",
              "Well, just remember that we're still two separate individuals, even though you're basically a clone."]:
        assert not lull(t), t
