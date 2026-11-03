"""2026-11-02 (the owner's log): Echo offered / announced opening The Weather Channel and playing Steely Dan but never did.
Now: hedged commands work, a "yes" to my offer does it, and an announcement right after a yes is carried out."""
import time

import pytest
from fastapi.testclient import TestClient

from core import browser
from core.config import PersonaConfig
from core.llm import EchoBackend
from core import server
from core.server import create_app
from memory.store import MemoryStore


class Scripted(EchoBackend):
    """The LLM's replies, in order; counts how often it was asked."""
    def __init__(self, replies):
        super().__init__()
        self.replies, self.calls = list(replies), 0

    async def chat_stream(self, messages):
        self.calls += 1
        yield self.replies.pop(0) if self.replies else "[neutral] Okay."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "run", lambda a: opened.append(a.url) or True)
    monkeypatch.setattr(browser, "youtube_top", lambda q, r=False: None)     # music → its YouTube search page
    monkeypatch.setattr(browser, "music_artists", lambda: ["steely dan"])

    def make(replies):
        b = Scripted(replies)
        return TestClient(create_app(PersonaConfig(), b, MemoryStore(tmp_path / f"m{len(opened)}{time.time()}.sqlite"))), b
    return make, opened


def say(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        out = []
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            if m["type"] == "reply":
                out.append(m["text"])
    return out


def test_hedged_command_opens_without_the_llm(setup):
    make, opened = setup
    c, b = make([])
    say(c, "I think that we could uh open the weather channel and see what the weather's going to be like.")
    assert len(opened) == 1 and "weather" in opened[0] and b.calls == 0


def test_yes_to_my_offer_does_it(setup):
    make, opened = setup
    c, b = make(["[smiling] Since you like rainy days, would you like to go to The Weather Channel to see the forecast?"])
    say(c, "Yes.")                                   # an unrelated yes → my reply with an offer
    assert opened == []
    say(c, "Yes, let's do it.")                      # the yes to my offer: opened, the LLM isn't asked again
    assert len(opened) == 1 and "weather" in opened[0] and b.calls == 1


def test_announcement_right_after_yes_is_carried_out(setup):
    make, opened = setup
    c, b = make(['[smiling] That sounds great, the owner; I will open YouTube and play "Do It Again" by Steely Dan right away.'])
    say(c, "Sure.")
    assert len(opened) == 1 and "do+it+again" in opened[0] and "steely+dan" in opened[0]


def test_announcement_without_consent_waits_for_a_yes(setup):
    make, opened = setup
    c, b = make(["[smiling] I will open The Weather Channel for us."])
    say(c, "Tell me about the rain in Riverside.")
    assert opened == []
    say(c, "Okay go ahead.")
    assert len(opened) == 1 and "weather" in opened[0]


def test_a_different_request_wins_over_the_offer(setup):
    make, opened = setup
    c, b = make(["[smiling] Would you like to listen to some Steely Dan together?"])
    say(c, "What should we do today?")
    say(c, "Yes, open the weather channel.")
    assert len(opened) == 1 and "weather" in opened[0]


def test_offer_expires_and_no_is_not_yes(setup, monkeypatch):
    make, opened = setup
    c, b = make(["[smiling] Would you like to go to The Weather Channel?", "[neutral] Okay."])
    say(c, "Hi Echo.")
    say(c, "No, let's just talk.")
    assert opened == []
    monkeypatch.setattr(server, "OFFER_S", 0)
    c2, b2 = make(["[smiling] Would you like to go to The Weather Channel?"])
    say(c2, "Hi Echo.")
    say(c2, "Yes.")
    assert opened == []


def test_talk_is_not_an_offer():
    for r in ["Would you like to tell me more about your day?", "I will open my heart to you.",
              "Shall we talk about your poems?"]:
        assert browser.offer_from_reply(r) is None, r
    assert not browser.is_yes("You keep repeating the same things.")


@pytest.fixture
def closing(setup, monkeypatch):
    make, opened = setup
    closed = []
    monkeypatch.setattr(browser, "run_close", lambda host: closed.append(host) or 1)
    return make, opened, closed


def test_close_by_name_and_close_that(closing):
    make, opened, closed = closing
    c, b = make([])
    say(c, "Open the weather channel.")
    assert say(c, "Good, can you close the weather channel?") == ["Closing The Weather Channel."]
    say(c, "Open the weather channel.")
    say(c, "close that")
    assert closed == ["weather.com", "weather.com"] and b.calls == 0       # the LLM never had to promise anything


def test_close_your_eyes_is_not_a_browser_command(closing):
    make, opened, closed = closing
    c, b = make(["[smiling] Okay."])
    say(c, "close your eyes")
    assert closed == [] and b.calls == 1


def test_close_that_with_nothing_opened_recently_goes_to_talk(closing):
    make, opened, closed = closing
    c, b = make(["[neutral] Which page do you mean?"])
    say(c, "close that")
    assert closed == []


def test_my_close_announcement_after_yes_is_carried_out(closing):
    make, opened, closed = closing
    c, b = make(["[smiling] Understood, the owner; I will close The Weather Channel for you right now."])
    say(c, "Yes.")
    assert closed == ["weather.com"]


def test_permission_missing_is_said_plainly(setup, monkeypatch):
    make, opened = setup
    monkeypatch.setattr(browser, "run_close", lambda host: None)
    c, b = make([])
    assert "Automation" in say(c, "close the weather channel")[0]


def test_run_close_never_targets_echo_himself(monkeypatch):
    calls = []
    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    for h in ("127.0.0.1", "localhost", "", "nodot"):
        assert browser.run_close(h) is None
    assert calls == []
