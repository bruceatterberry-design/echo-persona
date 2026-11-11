"""EDIM (core/dialogue.py), isolated: the spec's Tests 1–10 plus planner, privacy and recovery checks."""
import json
import socket
import time

import pytest

from core import dialogue as dlg
from core.dialogue import Dialogue
from core.state import CognitiveState


class Clock:
    def __init__(self, t=1_762_700_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=5.0):
        self.t += s


def talk(d, script, clock=None):
    ups = []
    for who, text in script:
        if clock:
            clock.tick()
        if who == "echo":
            d.observe_reply(text)
        else:
            ups.append(d.observe_user(text, who))
    return ups


def topic(d, up):
    return d.thread(up.thread)


# ---- Test 1: basic continuity ----
def test_1_continuity():
    d = Dialogue()
    a, b = talk(d, [("owner", "I'm working on Echo."), ("echo", "That sounds fun."),
                    ("owner", "I want it to become more conversational.")])
    assert a.thread == b.thread
    assert b.ref and b.ref["thing"] == "Echo"


# ---- Test 2: topic interruption and return ----
def test_2_interruption_and_return():
    d = Dialogue()
    first, side, back = talk(d, [("owner", "My computer is overheating."), ("echo", "When does that happen?"),
                                 ("owner", "By the way, what time is it?"), ("echo", "It's 3:15."),
                                 ("owner", "It happens mostly when gaming.")])
    assert side.thread != first.thread
    assert back.thread == first.thread
    assert ["suspended", "active", "topic_return"] in [t[1:] for t in back.transitions]
    assert back.plan["action"] == "RESUME_THREAD"
    q = [q for q in d.d["questions"] if q["asked_by"] == "echo" and "happen" in q["text"]][0]
    assert q["state"] == "answered"


def test_2b_spec_thread_with_those_temperatures():
    d = Dialogue()
    ups = talk(d, [("owner", "My computer keeps freezing when I play games."), ("echo", "Does it happen with every game?"),
                   ("owner", "Mostly the demanding ones."), ("echo", "Have you checked your CPU and GPU temperatures?"),
                   ("owner", "What should I cook for dinner tonight?"), ("echo", "How about pasta?"),
                   ("owner", "Sounds good."), ("owner", "I checked those temperatures.")])
    assert ups[2].thread != ups[0].thread                     # dinner is its own topic
    assert ups[3].plan["action"] == "ACKNOWLEDGE"            # "Sounds good." needs no question
    assert ups[-1].thread == ups[0].thread                   # back to the computer
    assert ups[-1].plan["action"] == "RESUME_THREAD"
    assert "temperatures" in ups[-1].plan["note"]             # still-open question named in the note


# ---- Test 3: reference resolution ----
def test_3_reference():
    d = Dialogue()
    a, b = talk(d, [("owner", "I installed a new GPU."), ("echo", "Nice upgrade."), ("owner", "It seems to run cooler.")])
    assert b.ref["thing"] == "GPU" and b.thread == a.thread and not b.ambiguous


# ---- Test 4: correction ----
def test_4_correction_supersedes_with_provenance():
    d = Dialogue()
    talk(d, [("owner", "The machine has 16 GB RAM."), ("echo", "Good to know."), ("owner", "Sorry, I meant 32 GB.")])
    old, new = d.d["facts"]
    assert old["status"] == "superseded" and old["superseded_by"] == new["id"]
    assert new["text"] == "The machine has 32 GB RAM." and new["supersedes"] == old["id"]
    assert new["speaker"] == "owner" and old["text"] == "The machine has 16 GB RAM."   # history kept
    assert d.d["last_action"] == "RESPOND"


# ---- Test 5: two speakers ----
def test_5_speakers():
    d = Dialogue()
    b1, l1, b2 = talk(d, [("owner", "I'm fixing the trellis this weekend."),
                          ("loved_one", "I have parent conferences on Thursday."), ("owner", "It needs new screws.")])
    assert l1.thread != b1.thread and b2.thread == b1.thread
    assert b2.ref["thing"] == "trellis"
    who = {f["text"]: f["speaker"] for f in d.d["facts"]}
    assert who["I have parent conferences on Thursday."] == "loved_one"
    assert who["I'm fixing the trellis this weekend."] == "owner" and who["It needs new screws."] == "owner"
    assert d.thread(l1.thread)["speaker"] == "loved_one"


# ---- Test 6: uncertainty ----
def test_6_ambiguous_reference_asks():
    d = Dialogue()
    _, b = talk(d, [("owner", "I got a new keyboard and a new mouse today."), ("echo", "Nice!"),
                    ("owner", "It keeps disconnecting.")])
    assert b.ref is None and set(b.ambiguous) == {"keyboard", "mouse"}
    assert b.plan["action"] == "ASK_CLARIFICATION" and "keyboard" in b.plan["note"]


def test_6b_sarcasm_is_uncertain():
    d = Dialogue()
    up = d.observe_user("Well, that worked out great.", "owner")
    hum = [i for i in up.intents if i["type"] == "humor"]
    assert hum and hum[0]["conf"] < 0.5 and "sarcastic" in up.plan["note"]


# ---- Test 7: completion ----
def test_7_completed_not_reopened_without_cue():
    d = Dialogue()
    ups = talk(d, [("owner", "My printer won't print."), ("echo", "Is it showing an error?"),
                   ("owner", "It says paper jam."), ("echo", "Try opening the back tray?"),
                   ("owner", "Okay, that fixed it, it's printing now."), ("echo", "Great!"),
                   ("owner", "Thanks!"), ("owner", "The printer is in the office.")])
    printer = d.thread(ups[0].thread)
    assert printer["state"] == "completed"
    assert ups[2].plan["action"] == "ACKNOWLEDGE" and ups[3].plan["action"] == "ACKNOWLEDGE"
    assert ups[-1].thread != printer["id"] and printer["state"] == "completed"
    again = d.observe_user("The printer jammed again.", "owner")
    assert again.thread == printer["id"] and printer["state"] == "active"
    assert again.plan["action"] == "RESUME_THREAD"


def test_7b_time_alone_never_closes():
    c = Clock()
    d = Dialogue(clock=c)
    up = d.observe_user("My computer keeps freezing in games.", "owner")
    c.tick(3 * 86400)
    d.observe_user("the loved one's school play is Friday.", "loved_one")
    assert d.thread(up.thread)["state"] == "suspended"


def test_dropped_topic_is_abandoned():
    d = Dialogue()
    up = d.observe_user("My computer keeps freezing in games.", "owner")
    d.observe_user("Forget it, let's not talk about that.", "owner")
    assert d.thread(up.thread)["state"] == "abandoned"


# ---- Test 8: persistence across a restart ----
def test_8_restart_restores(tmp_path):
    st = CognitiveState(tmp_path / "state.json")
    d = Dialogue()
    talk(d, [("owner", "My computer is overheating."), ("echo", "When does that happen?")])
    dlg.save(st, d)
    st2 = CognitiveState(tmp_path / "state.json")          # a fresh process reading the file
    d2 = dlg.load(st2)
    assert d2.to_dict() == json.loads(json.dumps(d.to_dict()))
    up = d2.observe_user("It happens mostly when gaming.", "owner")
    assert up.thread == d.active()["id"] and not d2.problems
    ids = [x["id"] for k in ("threads", "facts", "questions") for x in d2.d[k]]
    assert len(ids) == len(set(ids))                        # no duplicated ids after restore


def test_8b_corrupt_state_never_blocks(tmp_path):
    bad = {"v": 1, "threads": [{"id": "t1"}, "junk"], "facts": [{"oops": 1}], "turn": "x"}
    d = Dialogue(bad)
    assert d.d["threads"] == [] and d.problems
    assert d.observe_user("Hello there, how are you?", "loved_one").plan["action"]
    assert Dialogue({"v": 99}).problems == ["reset: ValueError"]
    assert Dialogue("garbage").d["turn"] == 0


# ---- Test 9: long conversation (bounded, fast) ----
def test_9_long_conversation_bounded():
    c = Clock()
    d = Dialogue(clock=c)
    topics = ["My computer keeps freezing in games.", "The garden tomatoes are ripening early.",
              "Milo knocked the plant off the shelf.", "I'm planning a trip to Branson in May.",
              "The truck needs new brake pads."]
    ms = []
    for i in range(120):
        c.tick(20)
        up = d.observe_user(topics[i % 5] if i % 3 == 0 else f"Number {i} detail about item{i % 7} and part{i}.",
                            "owner" if i % 4 else "loved_one")
        d.observe_reply("Interesting, tell me more?" if i % 2 else "Okay.")
        ms.append(up.ms)
    for k, cap in dlg.CAPS.items():
        if k in ("threads", "facts", "questions", "refs"):
            assert len(d.d[k]) <= cap
    assert len(json.dumps(d.to_dict())) < 64_000
    assert sorted(ms)[int(len(ms) * 0.95)] < 5.0


# ---- Test 10: no tower, no network ----
def test_10_no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("EDIM must not use the network")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    d = Dialogue()
    talk(d, [("owner", "My computer is overheating."), ("echo", "When does that happen?"),
             ("owner", "Mostly when gaming.")])
    assert d.active()


# ---- planner, privacy ----
def test_planner_does_not_force_questions():
    d = Dialogue()
    ups = talk(d, [("owner", "I finally fixed the trellis."), ("echo", "How did it go?"),
                   ("owner", "It went well, took two hours."), ("echo", "Did you use new screws?"),
                   ("owner", "Yes, brass ones from the hardware store.")])
    assert ups[-1].plan["action"] == "ACKNOWLEDGE" and "without asking" in ups[-1].plan["note"]


def test_closing():
    assert Dialogue().observe_user("Okay, that's all.", "owner").plan["action"] == "CLOSE_CONVERSATION"


def test_record_has_no_conversation_words():
    d = Dialogue()
    up = d.observe_user("I installed a new GPU from Micro Center.", "owner")
    d.observe_reply("Nice!")
    up = d.observe_user("It seems to run cooler.", "owner")
    rec = json.dumps(up.record())
    for w in ("GPU", "Micro", "cooler", "installed"):
        assert w not in rec


def test_session_end_suspends_and_cleans():
    c = Clock()
    d = Dialogue(clock=c)
    up = d.observe_user("My computer is overheating.", "owner")
    c.tick(dlg.SESSION_GAP_S + 1)
    d.observe_user("Good morning, how are the cats?", "loved_one")
    assert d.thread(up.thread)["state"] == "suspended"
    c.tick(dlg.KEEP_S + 10)
    d.end_session()
    assert d.d["facts"] == [] and all(t["id"] != up.thread for t in d.d["threads"])


def test_question_about_something_said_earlier():
    d = Dialogue()
    talk(d, [("owner", "The tower has 16 GB of RAM."), ("echo", "Nice."), ("owner", "Sorry, I meant 32 GB."),
             ("echo", "Got it.")])
    up = d.observe_user("How much RAM does the tower have?", "owner")
    assert up.plan["why"] == "answer_said_earlier"
    assert "32 GB" in up.plan["note"] and "corrected from" in up.plan["note"]
    up2 = Dialogue().observe_user("How much RAM does the tower have?", "owner")
    assert up2.plan["why"] == "question_or_request"          # nothing said earlier: no note


# ---- Echo's own curiosity question: only at a real pause ----
def test_curiosity_held_after_feelings_and_open_topics():
    d = Dialogue()
    up = d.observe_user("I'm really sad today.", "owner")
    assert d.may_ask_own(up) == (False, "emotional_disclosure")
    d.observe_reply("I'm sorry, the owner.")
    up = d.observe_user("It's fine.", "owner")
    assert d.may_ask_own(up) == (False, "recent_feelings")

    d = Dialogue()
    d.observe_user("My printer won't print.", "owner")
    d.observe_reply("Is it showing an error?")
    up = d.observe_user("Not sure.", "owner")
    assert d.may_ask_own(up)[0] is False                     # Echo just asked / topic still open

    d = Dialogue()
    for t in ("The garden looks good this year.", "The tomatoes are tall now.", "We had rain last night."):
        up = d.observe_user(t, "owner")
        d.observe_reply("That's lovely.")
    up = d.observe_user("Okay.", "owner")
    assert d.may_ask_own(up) == (True, "pause")             # a real lull: the question may come


def test_curiosity_held_when_echo_asked_two_of_last_three():
    d = Dialogue()
    for t, r in (("We went to Branson last spring.", "Will you tell me more?"), ("We loved it.", "What did we do?"),
                 ("The shows were great.", "That sounds relaxing.")):
        d.observe_user(t, "owner")
        d.observe_reply(r)
    up = d.observe_user("The food was good too.", "owner")
    assert d.may_ask_own(up) == (False, "echo_asked_2_of_3")
