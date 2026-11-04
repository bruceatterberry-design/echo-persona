"""2026-11-03 (the owner's log): Echo played Steely Dan, YouTube went on by itself (Reelin' in the Years, then Aja) and Echo
never noticed; "what is playing right now?" → "nothing is playing". YouTube's opener policy makes Echo's page think the
music window closed at once, so Echo now follows the YouTube tab in Chrome itself."""
import time

from fastapi.testclient import TestClient

import core.server
from core import browser, music
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore

DO_IT = "https://www.youtube.com/watch?v=aQnW-MxAU6U"
AJA = "https://www.youtube.com/watch?v=ajaAJAaja00&list=RDaQnW"


def _until(ws, kind):
    m = ws.receive_json()
    while m["type"] != kind:
        m = ws.receive_json()
    return m


def test_follows_autoplay_answers_what_is_playing_then_talks_when_it_stops(tmp_path, monkeypatch):
    chrome = {"tabs": [(DO_IT, "(1) Steely Dan - Do It Again - YouTube")]}
    monkeypatch.setattr(browser, "youtube_tabs", lambda: list(chrome["tabs"]))
    monkeypatch.setattr(browser, "youtube_top", lambda q, r=False, *_: DO_IT)
    monkeypatch.setattr(core.server, "FOLLOW_EVERY", 0.05)
    monkeypatch.setattr(core.server, "FOLLOW_GRACE", 0.2)
    listened = []

    def fake_listen(vid, **kw):
        listened.append(vid)
        title = "Steely Dan - Do It Again" if vid == "aQnW-MxAU6U" else "Steely Dan - Aja"
        return music.Result(False, "", music.Info(id=vid, url="u", title=title, duration=0.1), kind="already")
    monkeypatch.setattr(music, "listen", fake_listen)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Would you open up Steely Dan and play a song, you pick."})
        assert ws.receive_json()["type"] == "music"
        _until(ws, "state")
        ws.send_json({"type": "music_closed", "vid": "aQnW-MxAU6U"})   # what the page says at once (opener policy)
        time.sleep(0.5)                                     # longer than the song: still playing, so no "That was"
        ws.send_json({"text": "what is playing right now?"})
        assert _until(ws, "reply")["text"] == "It's “Do It Again” by Steely Dan."
        chrome["tabs"] = [(AJA, "Steely Dan - Aja - YouTube")]   # YouTube moved on by itself
        time.sleep(0.4)
        ws.send_json({"text": "What song is this?"})
        assert _until(ws, "reply")["text"] == "It's “Aja” by Steely Dan."
        chrome["tabs"] = []                                  # the owner closes it
        _until(ws, "music_stop")
        after = _until(ws, "reply")
        assert after["text"].startswith("That was “Aja” by Steely Dan")
    assert listened == ["aQnW-MxAU6U", "ajaAJAaja00"]       # listened along to the new song too


def test_what_is_playing_with_nothing_open(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "youtube_tabs", lambda: [])
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "What's playing?"})
        assert _until(ws, "reply")["text"] == "Nothing is playing on YouTube in Chrome right now."


def test_now_playing_question_words():
    for t in ["what is playing right now?", "What song is this?", "who sings this?", "what are we listening to",
              "is this a new song?"]:
        assert browser.asks_now_playing(t), t
    for t in ["what's on your mind", "play another steely dan song", "what is your favorite song",
              "what did you think of that song", "what are you doing"]:
        assert not browser.asks_now_playing(t), t
    assert browser.tab_song("(2) Steely Dan - Aja - YouTube") == "Steely Dan - Aja"


def test_choose_us_another_song_perhaps_a_title():
    """22:42 log: "use the same open window and choose us another Steely Dan song. Perhaps Reelin' in the Years?"."""
    lp = {"artist": "steely dan", "vid": "aQnW-MxAU6U", "at": time.time()}
    a = browser.parse_play("can you use the same open window and choose us another steely dan song. Perhaps reelin in "
                           "the years?", recent="steely dan", last_play=lp)
    assert a and a.play == "reelin in the years steely dan" and a.exclude == ["aQnW-MxAU6U"]
    a = browser.parse_play("pick us another steely dan song", recent="steely dan", last_play=lp)
    assert a and a.play == "steely dan" and a.random
    a = browser.parse_play("play another one, maybe Peg", recent="steely dan", last_play=lp)
    assert a and a.play == "peg steely dan"
    a = browser.parse_play("choose us another song. Maybe something slower?", recent="steely dan", last_play=lp)
    assert a and a.play == "steely dan" and a.random      # "something slower" isn't a song title


def test_duck_music_results(monkeypatch):
    import subprocess
    import types
    calls = []

    def fake(code, out="", err=""):
        def run(args, **k):
            calls.append(args[-1])
            return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return run
    monkeypatch.setattr(browser.subprocess, "run", fake(0, "1\n"))
    assert browser.duck_music(True) == "ok" and "(true,0.25)" in calls[-1]
    monkeypatch.setattr(browser.subprocess, "run", fake(0, "0"))
    assert browser.duck_music(False) == "none" and "(false,0.25)" in calls[-1]
    monkeypatch.setattr(browser.subprocess, "run", fake(1, "", "Executing JavaScript through AppleScript is turned off."))
    assert browser.duck_music(True) == "js_off"
    _ = subprocess


def test_my_window_not_visible_falls_back_to_song_length(tmp_path, monkeypatch):
    """Echo's music window is in his own copy of Chrome, which AppleScript can't see: don't decide it stopped."""
    monkeypatch.setattr(browser, "youtube_tabs", lambda: [])
    monkeypatch.setattr(browser, "youtube_top", lambda q, r=False, *_: DO_IT)
    monkeypatch.setattr(core.server, "FOLLOW_EVERY", 0.05)
    monkeypatch.setattr(core.server, "FOLLOW_GRACE", 0.2)
    monkeypatch.setattr(music, "MAX_SECONDS", 1.2)
    song = music.Info(id="aQnW-MxAU6U", url="u", title="Steely Dan - Do It Again", duration=300)
    monkeypatch.setattr(music, "listen", lambda vid, **kw: music.Result(False, "", song, kind="already"))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "play Steely Dan on YouTube"})
        assert ws.receive_json()["type"] == "music"
        _until(ws, "state")
        ws.send_json({"type": "music_closed", "vid": "aQnW-MxAU6U"})   # the false "closed" ~1 s in: ignored
        t0 = time.time()
        time.sleep(0.5)
        ws.send_json({"text": "what is playing right now?"})
        said = _until(ws, "reply")["text"]
        assert said.startswith("The last song I put on was “Do It Again” by Steely Dan") and "can't see" in said
        _until(ws, "music_stop")
        assert time.time() - t0 > 0.8                       # waited for the song (capped at 1.2 s here), not at once
