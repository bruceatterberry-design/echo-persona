"""Through the real chat socket: "open YouTube and play…" opens the video, Echo listens along, stays quiet, then talks
about the song when it ends; no life questions right after a command."""
from fastapi.testclient import TestClient

from core import browser, music
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.curiosity import Curiosity
from memory.store import MemoryStore


def test_play_listen_along_then_talk(tmp_path, monkeypatch):
    opened, listened, asked = [], [], []
    monkeypatch.setattr(browser, "youtube_top", lambda q: "https://www.youtube.com/watch?v=aQnW-MxAU6U")
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **k: opened.append(args[-1]))
    song = music.Info(id="aQnW-MxAU6U", url="u", title="Steely Dan - Do It Again", duration=0.1)

    def fake_listen(vid, **kw):
        listened.append((vid, kw["along"]))
        return music.Result(True, "done", song, {"bpm": 123, "notes_per_second": 2.5, "brightness_hz": 2000,
                                                  "dynamic_range_db": 8, "key": "G major"}, kind="listened")
    monkeypatch.setattr(music, "listen", fake_listen)
    monkeypatch.setattr(music, "MAX_SECONDS", 0.3)            # the "song" ends quickly
    monkeypatch.setattr(Curiosity, "maybe_ask", lambda self, s, t, now=None: asked.append(t))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                                      # history
        ws.send_json({"text": "Hey Echo, open YouTube and play some Steely Dan"})
        got = [ws.receive_json() for _ in range(3)]
        assert got[0] == {"type": "music", "url": "https://www.youtube.com/watch?v=aQnW-MxAU6U", "vid": "aQnW-MxAU6U"}
        assert got[1]["type"] == "reply" and "steely dan" in got[1]["text"].lower()
        m = ws.receive_json()
        while m["type"] != "music_stop":                       # song over: the window closes the music, mic back on
            m = ws.receive_json()
        after = ws.receive_json()
        assert after["type"] == "reply" and after["text"].startswith("That was “Do It Again” by Steely Dan")
        assert "What did you think" in after["text"]
        ws.receive_json()                                      # state idle
        ws.send_json({"text": "I loved it"})                   # right after: no life question
        while ws.receive_json()["type"] != "reply":
            pass
    assert opened == []                                        # the owner's Chrome isn't used: Echo's own music window
    assert listened == [("aQnW-MxAU6U", True)]
    assert asked == []


def test_closing_the_music_window_early(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "youtube_top", lambda q: "https://www.youtube.com/watch?v=aQnW-MxAU6U")
    song = music.Info(id="aQnW-MxAU6U", url="u", title="Steely Dan - Do It Again", duration=300)
    monkeypatch.setattr(music, "listen", lambda vid, **kw: music.Result(True, "", song, {
        "bpm": 123, "notes_per_second": 2.5, "brightness_hz": 2000, "dynamic_range_db": 8, "key": "G major"}, kind="listened"))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "play Steely Dan on YouTube"})
        assert ws.receive_json()["type"] == "music"
        while ws.receive_json()["type"] != "state":
            pass
        import time as _t
        _t.sleep(0.3)                                          # the 5-minute song is "playing"…
        ws.send_json({"type": "music_closed", "vid": "aQnW-MxAU6U"})   # …and the owner closes the window
        m = ws.receive_json()
        while m["type"] != "reply":
            m = ws.receive_json()
        assert m["text"].startswith("That was “Do It Again”")   # straight away, not after 5 minutes


def test_life_questions_still_happen_without_a_command(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(Curiosity, "maybe_ask", lambda self, s, t, now=None: asked.append(t))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "How are you today?"})
        while ws.receive_json()["type"] != "reply":
            pass
    assert asked == ["How are you today?"]
