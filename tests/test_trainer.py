"""Train Echo (core/trainer.py): 👍/👎 logging, the yes/no training conversation, lessons, sign-in, Claude disconnect."""
import json

import pytest

from core import trainer as T


class FakeClaude:
    def __init__(self, logged_in=True):
        self.cfg = {"enabled": False, "jobs": {"documents": True}}
        self.calls, self.answers, self.signed_in, self.login_opened = [], [], logged_in, 0

    def config(self):
        return json.loads(json.dumps(self.cfg))

    def save_config(self, c):
        self.cfg = c

    def binary(self):
        return "/usr/local/bin/claude"

    def logged_in(self):
        return self.signed_in

    def open_login(self):
        self.login_opened += 1
        return True

    def ask(self, job, prompt, schema=None, timeout=None):
        assert job == "training" and self.cfg["jobs"].get("training"), "Claude used while disconnected"
        self.calls.append(prompt)
        return self.answers.pop(0) if self.answers else None


DIAG = {"diagnosis": "I promised to play music but nothing happened.", "options": [
    {"title": "Never promise actions", "kind": "lesson", "change": "I never say I did something unless it happened.", "why": "honesty"},
    {"title": "Add a play command", "kind": "code", "change": "Recognise 'play X on YouTube'.", "why": "missing feature"}]}
EVAL = {"verdict": "good", "evaluation": "It should stop false promises.", "final_change":
        "I never say I'm opening or playing something unless it really happened.", "side_effects": "None expected."}


@pytest.fixture
def tr(tmp_path):
    claude = FakeClaude()
    reloaded = []
    t = T.Trainer(tmp_path / "t.sqlite", tmp_path / "lessons.json", tmp_path / "REQ.md", claude,
                  instructions=lambda: "BASE INSTRUCTIONS", on_lessons=lambda: reloaded.append(1))
    return t, claude, reloaded, tmp_path


def run(step):
    """Follow slow (Claude) parts like the server does; return the list of Steps shown."""
    shown = []
    while step:
        shown.append(step)
        step = step.work() if step.work else None
    return shown


def test_nothing_logged_until_testing_is_on(tr):
    t, *_ = tr
    assert t.record("hi", "hello", None) is None
    assert "Testing is on" in t.handle("Please use Claude to evaluate Echo").say and t.testing
    assert t.record("hi", "hello", "owner") == 1


def test_full_training_loop(tr):
    t, claude, reloaded, tmp = tr
    t.handle("use Claude to evaluate Echo")
    fid = t.record("open youtube and play steely dan", "[smiling] Sure, playing it now!", "owner")
    assert t.vote(fid, "down", "it didn't open anything") is None        # logged, not training yet
    t.record("how are you", "Fine.", "owner")                            # never voted: not sent anywhere
    s = t.handle("Train Echo")
    assert "1 reply" in s.say and "Not my memories" in s.say and s.choices == ["Yes", "No"]
    assert not claude.cfg["jobs"].get("training")                         # not connected before yes
    claude.answers = [DIAG]
    shown = run(t.handle("yes"))
    assert claude.cfg["jobs"]["training"] and claude.cfg["enabled"]
    assert "Claude says: I promised" in shown[-1].say and shown[-1].choices[:2] == ["1", "2"]
    sent = claude.calls[0]
    assert "it didn't open anything" in sent and "BASE INSTRUCTIONS" in sent and "how are you" not in sent
    claude.answers = [EVAL]
    shown = run(t.handle("option 1"))
    assert "Should I make this change?" in shown[-1].say and "unless it really happened" in shown[-1].say
    assert not t.lessons()                                                # nothing changed before yes
    s = t.handle("yes")
    assert t.lessons(active=True)[0]["text"].startswith("I never say") and reloaded
    assert s.replay == {"text": "open youtube and play steely dan", "entry": fid}
    retest = t.record("open youtube and play steely dan", "Still wrong", "owner", replay_of=fid)
    claude.answers = [DIAG]
    shown = run(t.vote(retest, "down", "still promised"))
    assert "look again" in shown[0].say and "ATTEMPT 1" in claude.calls[-1] and "still promised" in claude.calls[-1]
    claude.answers = [EVAL]
    run(t.handle("1"))
    s = t.handle("yes")
    retest2 = t.record(s.replay["text"], "I can't play that by myself.", "owner", replay_of=fid)
    shown = run(t.vote(retest2, "up"))
    assert "fixed" in shown[0].say and "Testing is off, and Claude is disconnected" in shown[-1].say
    assert not t.testing and claude.cfg["jobs"]["training"] is False and not t.busy


def test_no_and_skip_and_code_requests(tr):
    t, claude, _, tmp = tr
    t.handle("use claude to evaluate echo")
    a = t.record("q1", "r1", None); t.vote(a, "down")
    b = t.record("q2", "r2", None); t.vote(b, "down")
    t.handle("train echo")
    assert "not now" in t.handle("no").say.lower() and not claude.calls
    t.handle("train echo")
    claude.answers = [DIAG]
    run(t.handle("yes"))
    claude.answers = [DIAG]
    shown = run(t.handle("skip"))                                         # q1 skipped → q2 diagnosed
    assert shown[0].say.startswith("Skipped") and t.state["entry"] == b
    claude.answers = [EVAL]
    run(t.handle("the second"))                                           # the code option
    shown = run(t.handle("yes"))
    assert "TRAINING_REQUESTS" in shown[0].say and "q2" in (tmp / "REQ.md").read_text()
    assert "1 skipped" in shown[-1].say and "1 needs code" in shown[-1].say


def test_declining_a_change_goes_back_to_choices(tr):
    t, claude, _, _ = tr
    t.handle("use claude to evaluate echo")
    t.vote(t.record("q", "r", None), "down")
    t.handle("train echo"); claude.answers = [DIAG]; run(t.handle("yes"))
    claude.answers = [EVAL]; run(t.handle("1"))
    s = t.handle("no")
    assert "won't make that change" in s.say and t.state["mode"] == "choose" and not t.lessons()


def test_stop_training_keeps_the_rest_and_disconnects(tr):
    t, claude, _, _ = tr
    t.handle("use claude to evaluate echo")
    t.vote(t.record("q", "r", None), "down")
    t.handle("train echo"); claude.answers = [DIAG]; run(t.handle("yes"))
    s = t.handle("stop training")
    assert "stopped" in s.say and claude.cfg["jobs"]["training"] is False and t.testing and len(t.pending()) == 1


def test_signin_needed(tr):
    t, claude, _, _ = tr
    claude.signed_in = False
    t.handle("use claude to evaluate echo")
    t.vote(t.record("q", "r", None), "down")
    t.handle("Train Echo")
    s = t.handle("yes")
    assert "sign in" in s.say and claude.login_opened == 1 and not claude.cfg["jobs"].get("training") and not t.busy
    assert "sign in" in t.handle("Please log into Claude Pro").say and claude.login_opened == 2
    claude.signed_in = True
    assert "already signed in" in t.handle("sign in to Claude").say


def test_claude_unreachable_pauses(tr):
    t, claude, _, _ = tr
    t.handle("use claude to evaluate echo")
    t.vote(t.record("q", "r", None), "down")
    t.handle("train echo")
    shown = run(t.handle("yes"))                                          # FakeClaude has no answer → None
    assert "couldn't reach Claude" in shown[-1].say and claude.cfg["jobs"]["training"] is False and not t.busy


def test_lessons_block_and_undo(tr):
    t, *_ = tr
    assert T.lessons_block(t.lessons_path) == ""
    t.add_lesson("Rule one.", None); t.add_lesson("Rule two.", None)
    assert T.lessons_block(t.lessons_path).endswith("- Rule one.\n- Rule two.")
    assert "Rule two" in t.handle("undo the last lesson").say
    assert "Rule two" not in T.lessons_block(t.lessons_path)


def test_commands_not_confused_with_talk(tr):
    t, *_ = tr
    for talk in ("I need to train for a marathon", "yes I think so", "1", "Do you know Claude Monet?"):
        assert t.handle(talk) is None, talk


def test_things_echo_said_on_his_own_can_be_fixed_without_replay(tr):
    t, claude, _, _ = tr
    t.handle("use claude to evaluate echo")
    fid = t.record("(I said this when a song I listened along to ended)", "That was Simple Man. It felt happy.", T.SELF)
    t.vote(fid, "down", "Simple Man isn't a happy song")
    t.handle("train echo"); claude.answers = [DIAG]; run(t.handle("yes"))
    assert "Nobody spoke" in claude.calls[0]
    claude.answers = [EVAL]; run(t.handle("1"))
    shown = run(t.handle("yes"))
    assert "nothing to replay" in shown[0].say and shown[0].replay is None and t.lessons(active=True)
    assert "Testing is off" in shown[-1].say


def test_any_kind_of_reply_can_be_trained(tr):
    """Not just YouTube: memory answers, web answers, everyday talk all go through the same loop."""
    t, claude, _, _ = tr
    t.handle("use claude to evaluate echo")
    for q, r in [("What's the loved one's favourite flower?", "Roses, of course!"), ("What does nps.gov say?", "It's about Yosemite."),
                 ("Good morning", "Good morning, Pat!")]:
        t.vote(t.record(q, r, "owner"), "down", "wrong")
    assert len(t.pending()) == 3 and "3 replies" in t.handle("Train Echo").say
