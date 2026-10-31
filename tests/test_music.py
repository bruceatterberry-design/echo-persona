"""Echo listens to music (core/music.py): approved list, no download unless approved, audio always deleted."""
import json
import subprocess

import numpy as np
import pytest

from core import music
from memory.learn import Learner
from memory.store import MemoryStore


def test_video_id_and_requests():
    vid = "dQw4w9WgXcQ"
    for u in (f"https://www.youtube.com/watch?v={vid}&t=10", f"youtu.be/{vid}", f"https://music.youtube.com/watch?v={vid}",
              f"https://m.youtube.com/shorts/{vid}"):
        assert music.video_id(u) == vid, u
    assert music.video_id("https://evil.com/watch?v=dQw4w9WgXcQ") is None
    assert music.video_id("https://youtube.com/watch?v=short") is None
    assert music.listen_request(f"Hey Echo, listen to this song https://www.youtube.com/watch?v={vid}.") == vid
    assert music.listen_request(f"what is https://youtu.be/{vid} about") is None         # no "listen" words
    assert music.add_request("Please add country to my music list") == "country"
    assert music.add_request("add some songs") is None


def test_list_and_add_genre(tmp_path):
    p = tmp_path / "music.json"
    p.write_text(json.dumps({"approved_genres": ["Blues"], "approved_artists": ["Steely Dan"], "keep_audio": False}))
    assert music.load_list(p)["approved_genres"] == ["blues"]
    assert music.add_genre(p, "Country") and not music.add_genre(p, "country")
    assert music.load_list(p)["approved_genres"] == ["blues", "country"]


def info(**kw):
    base = dict(id="AAAAAAAAAAA", url="https://www.youtube.com/watch?v=AAAAAAAAAAA", title="Do It Again",
                artist="Steely Dan", channel="Steely Dan - Topic", duration=200)
    return music.Info(**{**base, **kw})


APPROVED = {"approved_genres": ["classic rock", "blues", "classical"], "approved_artists": ["Steely Dan"], "keep_audio": False}


def test_approval_rules():
    never = lambda i, g: pytest.fail("model not needed for a listed artist")  # noqa: E731
    assert music.approve(info(), APPROVED, never)["why"] == "artist"
    r = music.approve(info(artist="X", channel="X", title="Song"), APPROVED,
                      lambda i, g: {"genre": "blues", "confidence": 0.9, "best_description": "blues"})
    assert r["ok"] and r["genre"] == "blues"
    r = music.approve(info(artist="X", channel="X", title="Song"), APPROVED,
                      lambda i, g: {"genre": "blues", "confidence": 0.4, "best_description": "blues?"})
    assert not r["ok"]                                                   # not confident enough
    r = music.approve(info(artist="X", channel="X", title="Song"), APPROVED,
                      lambda i, g: {"genre": "other", "confidence": 0.9, "best_description": "Country"})
    assert not r["ok"] and r["guess"] == "country"


def tone(seconds=24.0, sr=music.SR, bpm=120):
    """C major chord with a click on every beat, getting louder: a fake 'song'."""
    t = np.arange(int(seconds * sr)) / sr
    y = sum(np.sin(2 * np.pi * f * t) for f in (261.63, 329.63, 392.0)) / 3
    beat = (np.mod(t, 60 / bpm) < 0.02).astype(float) * np.random.default_rng(0).normal(0, 1, len(t))
    return ((0.2 * y + 0.3 * beat) * np.linspace(0.3, 1.0, len(t))).astype(np.float32)


def test_analyse_finds_tempo_key_and_shape():
    a = music.analyse(tone())
    assert abs(a["bpm"] - 120) <= 4 or abs(a["bpm"] - 60) <= 2 or abs(a["bpm"] - 240) <= 8
    assert a["key"] in ("C major", "A minor", "E minor", "G major")   # C-E-G chord: C major (or a close relative)
    assert a["shape"] == "builds up" and a["seconds"] == 24.0
    d = music.describe(a)
    assert set(d) == {"pace", "tone", "dynamics", "mood"}


@pytest.fixture
def world(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    L = Learner(store, tmp_path / "ds")
    lst = tmp_path / "music.json"
    lst.write_text(json.dumps(APPROVED))
    return store, L, lst, tmp_path / "tmp"


def fake_download(i, folder):
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / f"{i.id}.wav"
    pcm = tone().tobytes()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(music.SR), "-ac", "1", "-i", "-", str(wav)],
                   input=pcm, check=True)
    return wav


def test_listen_saves_notes_and_deletes_audio(world):
    store, L, lst, tmp = world
    heard = []
    res = music.listen("AAAAAAAAAAA", list_path=lst, tmp_root=tmp, learner=L, classify=None, by="owner",
                       stt_call=lambda seg: heard.append(len(seg)) or "la la the river runs down to the sea tonight my old friend and we sing",
                       fetch=lambda vid: info(), download=fake_download)
    assert res.ok and "Do It Again" in res.say and res.job and "happy" not in res.say
    assert not (tmp / "AAAAAAAAAAA").exists()                       # audio gone
    notes = [m.content for m in store.all() if m.kind == "knowledge"]
    assert any("beats per minute" in n and "youtube.com/watch?v=AAAAAAAAAAA" in n for n in notes)
    assert any("river runs" in n for n in notes) and heard
    assert L.jobs()[0]["kind"] == "music" and L.jobs()[0]["status"] == "done"
    again = music.listen("AAAAAAAAAAA", list_path=lst, tmp_root=tmp, learner=L, classify=None, stt_call=None,
                         fetch=lambda vid: pytest.fail("not again"), download=fake_download)
    assert not again.ok and "already" in again.say
    assert L.forget(res.job) == len(notes)                           # forget works like any learned document


def test_not_approved_means_no_download(world):
    store, L, lst, tmp = world
    res = music.listen("BBBBBBBBBBB", list_path=lst, tmp_root=tmp, learner=L, stt_call=None,
                       classify=lambda i, g: {"genre": "other", "confidence": 0.95, "best_description": "country"},
                       fetch=lambda vid: info(id=vid, artist="Someone", channel="Someone", title="Truck Song"),
                       download=lambda i, f: pytest.fail("must not download"))
    assert not res.ok and "country" in res.say and "add country to my music list" in res.say
    assert not store.all()


def test_too_long_and_failures_still_clean_up(world):
    store, L, lst, tmp = world
    res = music.listen("CCCCCCCCCCC", list_path=lst, tmp_root=tmp, learner=L, classify=None, stt_call=None,
                       fetch=lambda vid: info(id=vid, duration=4 * 3600), download=fake_download)
    assert not res.ok and "minutes" in res.say

    def broken(i, folder):
        fake_download(i, folder)
        raise RuntimeError("network died half way")
    res = music.listen("DDDDDDDDDDD", list_path=lst, tmp_root=tmp, learner=L, classify=None, stt_call=None,
                       fetch=lambda vid: info(id=vid), download=broken)
    assert not res.ok and not (tmp / "DDDDDDDDDDD").exists()


def test_classical_skips_lyrics(world):
    store, L, lst, tmp = world
    res = music.listen("EEEEEEEEEEE", list_path=lst, tmp_root=tmp, learner=L, by="owner",
                       classify=lambda i, g: {"genre": "classical", "confidence": 0.9, "best_description": "classical"},
                       stt_call=lambda seg: pytest.fail("no lyrics for classical"),
                       fetch=lambda vid: info(id=vid, artist="Musopen", channel="Musopen", title="Symphony No. 5"),
                       download=fake_download)
    assert res.ok and any("instrumental" in m.content for m in store.all())


def test_labels():
    assert music.Info(id="x", url="u", title="Steely Dan - Do It Again (Official Audio)", channel="Dixie Blue").label() == \
        "“Do It Again” by Steely Dan"
    assert music.Info(id="x", url="u", title="Garth Brooks - Friends in Low Places (Lyrics) | BUGG Lyrics").label() == \
        "“Friends in Low Places” by Garth Brooks"
    assert music.Info(id="x", url="u", title="Clair de Lune", channel="Musopen - Topic").label() == "“Clair de Lune” by Musopen"
    assert music.Info(id="x", url="u", title="t", track="Simple Man", artist="Lynyrd Skynyrd").label() == \
        "“Simple Man” by Lynyrd Skynyrd"


def test_after_song_words():
    a = {"bpm": 123, "notes_per_second": 2.5, "brightness_hz": 2000, "dynamic_range_db": 8, "key": "G major"}
    i = info()
    assert music.after_song(music.Result(True, "", i, a, kind="listened")) == \
        "That was “Do It Again” by Steely Dan. It felt lively and energetic, about 123 beats a minute. What did you think of it?"
    assert "listened to that one before" in music.after_song(music.Result(False, "", i, kind="already"))
    assert 'add country to my music list' in music.after_song(music.Result(False, "", i, kind="declined", guess="country"))
    assert "couldn't listen along" in music.after_song(music.Result(False, "", i))


def test_listening_along_to_a_known_song_still_knows_its_length(world):
    store, L, lst, tmp = world
    first = music.listen("AAAAAAAAAAA", list_path=lst, tmp_root=tmp, learner=L, classify=None, stt_call=None,
                         fetch=lambda vid: info(), download=fake_download)
    again = music.listen("AAAAAAAAAAA", list_path=lst, tmp_root=tmp, learner=L, classify=None, stt_call=None,
                         fetch=lambda vid: info(), download=lambda i, f: pytest.fail("no second download"), along=True)
    assert first.kind == "listened" and again.kind == "already" and again.info.duration == 200
