"""Echo's own phone calls, run against a fake phone line: the owner's rules, in order."""
from __future__ import annotations

import time
from contextlib import contextmanager

import numpy as np

from echolink.callagent import CallAgent, closing_line, opening


class Line:
    """What the other person says, one item per time Echo listens (None = silence)."""
    def __init__(self, said):
        self.said, self.now = list(said), ""


class FakeAudio:
    def __init__(self, line):
        self.line, self.muted, self.spoken = line, False, 0

    def start(self): pass
    def stop(self): pass
    def drain(self): pass

    def listen(self, wait_s, quiet_s=1.3, max_s=25, cancelled=lambda: False):
        x = self.line.said.pop(0) if self.line.said else None
        if x is None:
            return None
        self.line.now = x
        return np.ones(16000, dtype=np.float32)

    def speak(self, pcm, rate):
        self.spoken += 1

    def feed(self, pcm, rate): self.spoken += 1
    def flush(self): pass
    def wait_drained(self, cancelled=lambda: False): pass
    def hush(self): pass


class FakeResp:
    def __init__(self, j=None):
        self.j, self.status_code, self.headers = j, 200, {"X-Sample-Rate": "24000"}
    def json(self): return self.j
    def iter_raw(self): yield b"\0\0" * 24000


class FakeHttp:
    def __init__(self, line): self.line = line
    def post(self, url, **kw): return FakeResp({"text": self.line.now})

    @contextmanager
    def stream(self, *a, **kw): yield FakeResp()


class FakePhone:
    def __init__(self): self.log = []
    def route(self): self.log.append("route")
    def unroute(self): self.log.append("unroute")
    def dial(self, n): self.log.append("dial " + n)
    def hang_up(self): self.log.append("hang_up"); return True


class FakeBrain:
    """Follows the rules the real prompt asks for, so the call's flow can be checked."""
    def plan(self, name, request):
        return {"business": "to introduce myself", "question": "What do you think of the owner having an AI assistant?",
                "goal": "say hello", "opening": opening(name, "to introduce myself",
                                                         "What do you think of the owner having an AI assistant?")}

    def words(self, call, task):
        yield {"answer": "Good question. I'm the owner's assistant on his Mac.", "ack": "That's great to hear.",
               "answer_ack": "I live on the owner's Mac."}[task]

    def summary(self, call): return "Danny answered and liked it."


def run(said, tmp_path):
    line = Line(said)
    phone = FakePhone()
    a = CallAgent(tmp_path, brain=FakeBrain(), audio_factory=lambda: FakeAudio(line), phone=phone, http=FakeHttp(line))
    c = a.plan("Danny Ruiz", "+18154099542", "call Danny and introduce yourself")
    a.start(c.id)
    for _ in range(400):
        if c.state == "ended":
            break
        time.sleep(0.02)
    return c, phone


def test_opening_follows_owners_words():
    assert opening("Danny Ruiz", "to introduce myself", "What do you think") == \
        "Hello Danny, this is Echo. My owner wanted me to call you to introduce myself. What do you think?"
    assert opening("Sam", "the dinner plans", "Is six okay?").startswith("Hello Sam, this is Echo. My owner wanted me to call you about the dinner plans.")


def test_answer_question_close_wait_for_goodbye_then_hang_up(tmp_path):
    c, phone = run(["Hello?", "Ha, that's pretty cool, I like it.", "Okay, bye Echo!"], tmp_path)
    who = [x["who"] for x in c.lines]
    assert who == ["Danny", "echo", "Danny", "echo", "Danny"]
    assert c.lines[1]["text"].startswith("Hello Danny, this is Echo. My owner wanted me to call you")
    assert c.lines[3]["text"].endswith("Thank you for your time, Danny. Talk to you soon.")
    assert phone.log[0] == "route" and phone.log[1] == "dial +18154099542" and phone.log[-2:] == ["hang_up", "unroute"]
    assert c.summary and (tmp_path / "owner" / "notes.jsonl").read_text().count("Echo called Danny Ruiz") == 1


def test_follow_up_question_after_goodbye_keeps_talking(tmp_path):
    c, _ = run(["Hello?", "Sounds good.", "Wait, where do you live?", "Oh nice. Bye!"], tmp_path)
    echo = [x["text"] for x in c.lines if x["who"] == "echo"]
    # answer → goodbye; follow-up question → answered + goodbye again; their "bye" → hang up
    assert echo[1].endswith(closing_line("Danny Ruiz")) and echo[2].startswith("I live on the owner's Mac.")
    assert echo[2].endswith(closing_line("Danny Ruiz")) and len(echo) == 3 and c.lines[-1]["who"] == "Danny"


def test_no_answer_and_voicemail(tmp_path):
    c, phone = run([None], tmp_path)
    assert c.lines == [] or c.lines[-1]["text"] == "No answer."
    assert "hang_up" in phone.log
    c2, _ = run(["Hi, you've reached Danny, leave a message after the tone.", None], tmp_path)
    assert "He'll try you again soon" in c2.lines[-1]["text"]


def test_only_one_call_and_never_twice(tmp_path):
    line = Line(["Hello?"] + [None] * 5)
    a = CallAgent(tmp_path, brain=FakeBrain(), audio_factory=lambda: FakeAudio(line), phone=FakePhone(), http=FakeHttp(line))
    c = a.plan("Danny Ruiz", "+18154099542", "x")
    a.start(c.id)
    import pytest
    with pytest.raises(RuntimeError):
        a.start(c.id)


def test_reaction_rules():
    from echolink.callagent import reaction
    assert reaction("Ha, that's pretty cool.", closed=False) == "ack_close"
    assert reaction("Where do you live?", closed=False) == "answer"
    assert reaction("Okay, talk soon!", closed=False) == "close"
    assert reaction("Okay, bye!", closed=True) == "hang_up"
    assert reaction("Wait, what time Friday?", closed=True) == "answer_close"
    assert reaction("Sorry, who is this?", closed=False) == "repeat"
    assert reaction("There's no Danny here, you have the wrong number.", closed=False) == "wrong"


def test_greets_whoever_is_called_by_their_proper_name():
    from echolink.callagent import closing_line, opening
    assert opening("Sam Smith", "to say hi", "How are you").startswith("Hello Sam, this is Echo.")
    assert opening("Neighbor Jo Miller", "to say hi", "How are you").startswith("Hello Jo, this is Echo.")
    assert opening("Dr. Alex Grant", "about Friday", "Is that okay").startswith("Hello Dr. Grant, this is Echo.")
    assert opening("VZ Roadside Assistance", "about the car", "Can you help").startswith("Hello, this is Echo.")
    assert closing_line("Sam Smith") == "Thank you for your time, Sam. Talk to you soon."
    assert closing_line("Apple Inc.") == "Thank you for your time. Talk to you soon."


def test_reaction_bye_phrases_are_not_questions():
    from echolink.callagent import reaction
    assert reaction("Have a good day.", closed=True) == "hang_up"
    assert reaction("Do you need anything else?", closed=True) == "answer_close"


def test_voice_stream_is_continuous_with_reserve_and_refill():
    from echolink.callagent import CallAudio, RATE
    a = CallAudio.__new__(CallAudio)
    a.sd = type("SD", (), {"OutputStream": lambda *x, **k: type("O", (), {"start": lambda s: None,
                                                                           "stop": lambda s: None,
                                                                           "close": lambda s: None})(),
                           "query_devices": staticmethod(lambda: [{"name": "BlackHole 2ch", "max_output_channels": 2,
                                                                   "max_input_channels": 2}])})
    import numpy as np
    tone = (np.sin(np.arange(24000) * 2 * np.pi * 220 / 24000) * 8000).astype("<i2").tobytes()
    a.feed(tone[:4801], 24000)              # odd byte count: the half sample is kept for the next piece
    assert not a.gate                        # 0.1 s: still gathering the 0.5 s reserve
    a.feed(tone[4801:], 24000)
    assert a.gate and abs(a.pending_s() - 1.0) < 0.01     # 1 s of 24 kHz → 48 kHz, nothing lost or doubled
    joined = np.concatenate(a._q)
    assert np.max(np.abs(np.diff(joined))) < 0.02          # no click where the two pieces meet


def test_long_sentences_go_to_the_voice_in_short_phrases():
    from echolink.callagent import phrases
    long = ("They beat the Milwaukee Brewers in the National League Championship Series and then came back from a "
            "three to two deficit against Toronto, winning game seven in extra innings on a home run by Freddie Freeman.")
    out = phrases(long)
    assert len(out) >= 3 and all(len(p) <= 90 for p in out) and " ".join(out).split() == long.split()
    assert phrases("Short one.") == ["Short one."]
