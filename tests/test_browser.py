import pytest
from fastapi.testclient import TestClient

from core import browser
from core.config import ROOT, PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore

SHORTCUTS = browser.load_shortcuts(ROOT / "config" / "browser.json")
# the two-account example from the README: plain "YouTube" asks whose
TWO_ACCOUNTS = [
    {"say": "Sam's YouTube", "names": ["sam's youtube"], "url": "https://www.youtube.com/?authuser=sam@example.com"},
    {"say": "Pat's YouTube", "names": ["pat's youtube", "your youtube"], "url": "https://www.youtube.com/?authuser=me@example.com"},
    {"say": "YouTube", "names": ["youtube"], "ask": "Sam's YouTube or mine?",
     "answers": {"sam's": "Sam's YouTube", "sam": "Sam's YouTube", "yours": "Pat's YouTube"}},
]


def opens(text, shortcuts=SHORTCUTS):
    a = browser.parse(text, shortcuts)
    return a and (a.url if a.url is not None else a.script)


@pytest.mark.parametrize("text,expect", [
    ("open the weather channel", "https://weather.com/"),
    ("Open Chrome and go to the Weather Channel", "https://weather.com/"),
    ("show me the weather", "https://weather.com/"),
    ("hey echo, can you open up YouTube please", "https://www.youtube.com/"),
    ("open the voice studio", "scripts/open_studio.sh"),
    ("go to weather dot com slash maps", "https://weather.com/maps"),
    ("open a chrome window to example dot org", "https://example.org"),
    ("Open Chrome to https://www.example.org/page?x=1", "https://www.example.org/page?x=1"),
    ("echo, go to apple.com.", "https://apple.com"),
    ("Hey Eko, open the weather.", "https://weather.com/"),
])
def test_opens(text, expect):
    assert expect in (opens(text) or "")


@pytest.mark.parametrize("text", [
    "open up about your day", "show me how you make pancakes", "start a story about the lake",
    "what's the weather like", "I opened the door", "open your heart",
])
def test_conversation_is_left_alone(text):
    assert browser.parse(text, SHORTCUTS) is None


def test_unsure_asks_instead_of_guessing():
    for text in ("open chrome to the place with the cats", "go to the cat place"):
        a = browser.parse(text, SHORTCUTS)
        assert a.url is None and a.script is None and "address" in a.say


def test_account_shortcuts_and_follow_up_question():
    assert "sam@example.com" in opens("open Sam's YouTube", TWO_ACCOUNTS)
    assert "me@example.com" in opens("pull up your YouTube.", TWO_ACCOUNTS)
    a = browser.parse("open YouTube", TWO_ACCOUNTS)
    assert a.url is None and "mine?" in a.say and a.answers
    assert "sam@example.com" in browser.answer("Sam's", a.answers, TWO_ACCOUNTS).url
    assert "me@example.com" in browser.answer("yours.", a.answers, TWO_ACCOUNTS).url
    assert browser.answer("what time is it", a.answers, TWO_ACCOUNTS) is None


def test_only_web_addresses():
    assert browser.spoken_url("file:///etc/passwd") is None
    assert browser.spoken_url("javascript:alert(1)") is None
    assert browser.spoken_url("hello there") is None


def test_scripts_stay_inside_echo():
    assert browser.run(browser.Action(say="", script="../../../../bin/ls")) is False


def test_chat_opens_without_the_llm(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "run", lambda a: opened.append(a.url) or True)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "history"
        ws.send_json({"text": "Hey Echo, open the weather channel"})
        reply = ws.receive_json()
        assert reply["type"] == "reply" and reply["text"] == "Opening The Weather Channel."
        assert ws.receive_json()["state"] == "idle"
    assert opened == ["https://weather.com/"]


def test_play_on_youtube():
    from core import browser
    a = browser.parse_play("Hey Echo, open YouTube and play some Steely Dan.", None, [])
    assert a.play == "steely dan" and a.url == "https://www.youtube.com/results?search_query=steely+dan"
    assert browser.parse_play("play some music by the Eagles", None, []).play == "the eagles"
    assert browser.parse_play("play Hotel California on YouTube", None, []).play == "hotel california"
    assert browser.parse_play("play some Steely Dan", None, ["steely dan"]).play == "steely dan"    # listed artist
    assert browser.parse_play("play some Steely Dan", None, []) is None                             # unknown: just talk
    assert browser.parse_play("could you play some blues", None, ["blues"]).play == "blues"        # listed genre
    for t in ("play a game with me", "open youtube", "can you play that again", "let's play cards"):
        assert browser.parse_play(t, None, []) is None, t


def test_play_uses_the_configured_account(monkeypatch):
    from core import browser
    monkeypatch.setattr(browser, "LOVED_ONE_ACCOUNT", "sam@example.com")
    assert "authuser=sam@example.com" in browser.parse_play("play Clair de Lune on YouTube", "loved_one", []).url
    assert "authuser" not in browser.parse_play("play Clair de Lune on YouTube", "owner", []).url


def test_play_opens_top_video(monkeypatch):
    from core import browser
    opened = []
    monkeypatch.setattr(browser, "youtube_top", lambda q: "https://www.youtube.com/watch?v=aQnW-MxAU6U")
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **k: opened.append(args[-1]))
    assert browser.run(browser.parse_play("play Steely Dan on YouTube", None, []))
    assert opened == ["https://www.youtube.com/watch?v=aQnW-MxAU6U"]
    monkeypatch.setattr(browser, "youtube_top", lambda q: None)                    # lookup failed → search page
    browser.run(browser.parse_play("play Steely Dan on YouTube", None, []))
    assert "results?search_query=steely+dan" in opened[-1]


def test_natural_music_requests():
    from core import browser
    A = ["steely dan", "dan fogelberg", "blues"]
    for t, want in [("Would you open up a Steely Dan song and listen to it with me?", "steely dan"),
                    ("can we listen to some blues", "blues"), ("let's hear some Dan Fogelberg", "dan fogelberg"),
                    ("find me a Steely Dan song", "steely dan"), ("pull up Hotel California on YouTube", "hotel california")]:
        a = browser.parse_play(t, None, A)
        assert a and a.play == want, t
    for t in ("do you like to listen to blues?", "open up about your day", "listen to me",
              "what is your favourite Steely Dan song?", "can we listen to that again"):
        assert browser.parse_play(t, None, A) is None, t


def test_unrecognised_requests_are_flagged_but_talk_is_not():
    from core import browser
    assert browser.wants_action("open the spotify app and play jazz", [])
    assert browser.wants_action("play the radio", [])
    for t in ("open up about your day", "listen to me", "open the window", "I love you", "can we listen to that again"):
        assert not browser.wants_action(t, []), t
