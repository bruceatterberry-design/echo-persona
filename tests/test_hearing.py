"""Listening with the owner/the loved one through the room mic (core/hearing.py), 2026-11-03."""
import numpy as np
import scipy.signal as ss
from fastapi.testclient import TestClient

from core import hearing
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore

SR = hearing.SR


def tune(seed: int, seconds: float = 60) -> np.ndarray:
    """A made-up song: a random chord every half second (different seed = different song)."""
    rng = np.random.default_rng(seed)
    out, t = [], np.arange(int(SR * 0.5)) / SR
    for _ in range(int(seconds * 2)):
        notes = 220 * 2 ** (rng.choice(24, 3, replace=False) / 12)
        out.append(sum(np.sin(2 * np.pi * f * t) for f in notes) * np.hanning(len(t)))
    return (np.concatenate(out) * 0.2).astype(np.float32)


def through_room(y: np.ndarray, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    b, a = ss.butter(2, [120, 6000], btype="band", fs=SR)
    y = ss.lfilter(b, a, y)
    ir = rng.standard_normal(int(SR * 0.3)) * np.exp(-np.arange(int(SR * 0.3)) / (SR * 0.08))
    ir[0] = 1
    y = ss.fftconvolve(y, ir * 0.3)[: len(y)]
    return (y / np.max(np.abs(y)) * 0.4 + 0.01 * rng.standard_normal(len(y))).astype(np.float32)


def test_recognises_a_known_song_through_a_room_and_not_an_unknown_one(tmp_path):
    h = hearing.Hearing(tmp_path)
    for k in (1, 2, 3):
        h.library.add(f"Song {k}", "The Testers", hearing.song_print(tune(k)), vid=f"v{k}")
    h.start("owner")
    mic = through_room(tune(2)[SR * 20: SR * 44])
    got = [h.feed(mic[i: i + SR * 4]) for i in range(0, len(mic), SR * 4)]
    assert any(g.get("new") and g.get("song") == "“Song 2” by The Testers" for g in got)
    assert h.room.count == 1                                   # it tuned itself to the room from that song
    h.stop()
    h.start("owner")
    mic = through_room(tune(9)[: SR * 24])                     # never heard
    got = [h.feed(mic[i: i + SR * 4]) for i in range(0, len(mic), SR * 4)]
    assert not any(g.get("song") for g in got)
    assert "don't recognise it yet" in h.describe_now()


def test_room_curve_learns_a_fixed_room():
    room = hearing.Room.__new__(hearing.Room)
    room.path, room.curve, room.count, room.history = __import__("pathlib").Path("/dev/null"), np.zeros(40, np.float32), 0, []
    room.save = lambda: None
    true = np.linspace(6, -6, 40)                              # a room: boomy bass, dull treble
    rng = np.random.default_rng(0)
    errs = []
    for _ in range(6):
        ref = rng.normal(-30, 8, (40, 100))
        mic = ref + true[:, None] + 7 + rng.normal(0, 2, (40, 100))
        errs.append(room.learn(mic, ref))
    assert errs[0] > 2.5 and errs[-1] < 1                       # first session: ~3 dB off; later: close
    assert np.abs(room.curve - (true - true.mean())).mean() < 0.5


def test_skips_talking_above_the_musics_own_level():
    """Real room: music alone scored 0.76-0.79 on the owner's voice print; only a clear rise means someone is talking."""
    level = {"v": 0.77}
    h = hearing.Hearing(__import__("tempfile").mkdtemp(), speaker_scores=lambda a: {"owner": level["v"]})
    h.start("owner")
    for v in (0.77, 0.78, 0.76, 0.79):                         # music only: never skipped
        level["v"] = v
        assert h.feed(tune(1, 4))["skipped"] is False
    level["v"] = 0.93                                          # the owner talks over it
    assert h.feed(tune(1, 4))["why"] == "talking"
    assert h.session.music_s == 16


def test_words():
    assert hearing.asks_start("Echo, listen with me")
    assert hearing.asks_start("can you listen to this with me?")
    assert not hearing.asks_start("listen to some steely dan with me")   # that's "play it"
    assert not hearing.asks_start("listen to me")
    assert hearing.asks_stop("stop listening with me")
    assert hearing.teaching("No, it's Peg by Steely Dan.") == ("Peg", "Steely Dan")
    assert hearing.teaching("that was great by the way") is None
    assert hearing.asks_what("what do you hear?")


def test_through_the_chat_socket(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    lib = hearing.Library(tmp_path / "hearing" / "library.sqlite")
    lib.add("Aja", "Steely Dan", hearing.song_print(tune(4)), vid="aja")

    def until(ws, kind):
        m = ws.receive_json()
        while m["type"] != kind:
            m = ws.receive_json()
        return m
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Echo, listen with me"})
        assert until(ws, "hear") == {"type": "hear", "on": True}
        assert "listening with you" in until(ws, "reply")["text"]
        mic = through_room(tune(4)[SR * 10: SR * 30])
        for i in range(0, len(mic), SR * 4):
            r = client.post("/api/hearing/chunk", content=mic[i: i + SR * 4].astype("<f4").tobytes())
            assert r.status_code == 200 and r.json()["active"]
        assert until(ws, "note")["text"] == "🎧 I think this is “Aja” by Steely Dan."
        ws.send_json({"text": "What do you hear?"})
        assert until(ws, "reply")["text"].startswith("I think this is “Aja” by Steely Dan.")
        ws.send_json({"text": "stop listening"})
        assert until(ws, "hear") == {"type": "hear", "on": False}
        said = until(ws, "reply")["text"]
        assert said.startswith("Thanks for listening with me.") and "Aja" in said
    assert client.post("/api/hearing/chunk", content=b"\0" * 400).json() == {"active": False}


# ---- part 2: the parts of the music, the sung words, a song found from a few of them ----
class FakeSeparator:
    def __init__(self):
        self.calls = 0

    def split(self, y, sr=SR):
        self.calls += 1
        n = len(y)
        return {"vocals": y * 0.5, "drums": y * 0.3, "bass": y * 0.1, "other": y * 0.1}

    def release_if_idle(self):
        return False


def test_unknown_song_found_from_the_sung_words(tmp_path):
    searched = []

    def lookup(phrase):
        searched.append(phrase)
        return ["Steely Dan - Do It Again Lyrics | Genius Lyrics", "Steely Dan - Do It Again Lyrics | AZLyrics.com",
                "Waylon Jennings - Do It Again Lyrics | AZLyrics.com"]
    sep = FakeSeparator()
    h = hearing.Hearing(tmp_path, stt_sync=lambda a: "go back jack do it again wheel turning round and round",
                        may_split=lambda: True, lookup=lookup, separator=sep)
    h.start("owner")
    mic = through_room(tune(5)[: SR * 24])
    got = [h.feed(mic[i: i + SR * 4]) for i in range(0, len(mic), SR * 4)]
    assert sep.calls == 1                                       # after 20 s of music
    g = next(x for x in got if x.get("guess"))
    assert g["guess"] == "“Do It Again” by Steely Dan"
    assert 1 <= len(searched) <= 3 and all(len(p.split()) == 5 for p in searched)   # a few words each, nothing else
    said = h.describe_now()
    assert said.startswith("From the words I could make out, it might be “Do It Again” by Steely Dan. Is that right?")
    assert "someone's singing" in said
    assert h.confirm_guess().startswith("Thank you. I'll remember how “Do It Again” by Steely Dan sounds")
    assert any(x["title"] == "Do It Again" and x["source"] == "mic" for x in h.library.songs())


def test_no_guess_when_the_results_disagree(tmp_path):
    h = hearing.Hearing(tmp_path, stt_sync=lambda a: "mountain roll back to the grave and the river runs",
                        may_split=lambda: True, separator=FakeSeparator(),
                        lookup=lambda p: ["Back to the grave lyrics", "Marvin Morrow lyrics - Search"])
    h.start("owner")
    mic = through_room(tune(6)[: SR * 24])
    got = [h.feed(mic[i: i + SR * 4]) for i in range(0, len(mic), SR * 4)]
    assert not any(x.get("guess") for x in got) and h.session.guess is None


def test_waits_while_we_are_talking(tmp_path):
    sep = FakeSeparator()
    h = hearing.Hearing(tmp_path, stt_sync=lambda a: "", may_split=lambda: False, separator=sep, lookup=None)
    h.start("owner")
    mic = through_room(tune(7)[: SR * 24])
    for i in range(0, len(mic), SR * 4):
        h.feed(mic[i: i + SR * 4])
    assert sep.calls == 0                                       # conversation first: no splitting


def test_phrases_and_votes():
    ph = hearing.lyric_phrases(["Jack, do it again. Wheel turn and round and round you go back.",
                                "In the morning, you go garden for the man who stole your water?"])
    assert ph and all(len(p.split()) == 5 for p in ph)
    assert hearing.song_from_hits([["Back to the grave lyrics", "Lyric Finder: Song Finder by Lyrics"]]) is None
    assert hearing.song_from_hits([["Steely Dan - Peg Lyrics | Genius Lyrics", "Steely Dan - Peg Lyrics | AZLyrics.com"],
                                   ["Steely Dan - Peg Lyrics | Genius Lyrics"]])["title"] == "Peg"


# ---- part 3: who's playing (learned from the owner/the loved one, guessed only after two confirmations) ----
def drums_like(seed: int, swung: bool, seconds: float = 24) -> np.ndarray:
    rng = np.random.default_rng(seed)
    beat = 0.5
    y = np.zeros(int(SR * seconds), np.float32)
    t = 0.0
    while t < seconds - 0.2:
        for off in ((0.0, 0.66) if swung else (0.0, 0.5)):
            k = int((t + off * beat) * SR)
            hit = rng.standard_normal(800) * np.exp(-np.arange(800) / 120)
            y[k:k + 800] += hit[: len(y[k:k + 800])]
        t += beat
    return y / np.max(np.abs(y)) * 0.5


class VoiceSeparator(FakeSeparator):
    def __init__(self, voice):
        super().__init__()
        self.voice = voice

    def split(self, y, sr=SR):
        self.calls += 1
        return {"vocals": y * 0.6, "drums": drums_like(1, self.voice == "swing", len(y) / SR)[: len(y)],
                "bass": y * 0.05, "other": y * 0.3}


def session_with(tmp_path, voice_vec, voice="straight", seed=5):
    h = hearing.Hearing(tmp_path, stt_sync=lambda a: "", may_split=lambda: True, lookup=None,
                        separator=VoiceSeparator(voice), voice_embed=lambda a: voice_vec)
    h.start("owner")
    mic = through_room(tune(seed)[: SR * 24])
    for i in range(0, len(mic), SR * 4):
        h.feed(mic[i: i + SR * 4])
    return h


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def test_learns_a_singer_and_only_guesses_after_two_confirmations(tmp_path):
    fagen = unit(np.r_[np.ones(8), np.zeros(8)])
    h = session_with(tmp_path, fagen)
    assert h.who("singer").startswith("I can't tell who it is yet")
    assert hearing.teaching_who("That's Donald Fagen singing.") == ("Donald Fagen", "singer")
    assert h.teach_who("Donald Fagen", "singer").endswith("Once more on another song and I'll start recognising it.")
    h.stop()
    h = session_with(tmp_path, fagen, seed=6)
    assert h.clue("singer") is None                             # confirmed once: no guessing yet
    assert "That's 2 times now" in h.teach_who("Donald Fagen", "singer")
    h.stop()
    h = session_with(tmp_path, unit(fagen + 0.1), seed=7)        # a third song, a slightly different take
    assert h.who("singer") == "The singer sounds like Donald Fagen, but that's my guess. Am I right?"
    h.stop()
    other = unit(np.r_[np.zeros(8), np.ones(8)])
    h = session_with(tmp_path, other, seed=8)                    # somebody else
    assert h.clue("singer") is None


def test_drum_feel_words():
    _v, straight = hearing.drum_style(drums_like(1, False), drums_like(1, False))
    _v, swung = hearing.drum_style(drums_like(1, True), drums_like(1, True))
    assert "straight" in hearing.drum_words(straight)
    assert swung["swing"] > straight["swing"]


def test_who_words():
    assert hearing.asks_who("who's on drums?") == "drummer"
    assert hearing.asks_who("who is singing") == "singer"
    assert hearing.teaching_who("the drummer is Jeff Porcaro") == ("Jeff Porcaro", "drummer")
    assert hearing.teaching_who("Walter Becker on guitar") == ("Walter Becker", "guitarist")
    assert hearing.teaching_who("that's it singing") is None


# ---- 2026-11-04: "who was that?" right after "stop listening" (the owner: Echo said "I couldn't hear the track myself")
def test_who_was_that_after_stop_is_answered_from_what_i_heard(tmp_path):
    h = hearing.Hearing(tmp_path)
    h.library.add("Aja", "Steely Dan", hearing.song_print(tune(4)), vid="aja")
    h.start("owner")
    mic = through_room(tune(9)[SR * 10: SR * 40])          # a song I don't know
    for i in range(0, len(mic), SR * 4):
        h.feed(mic[i: i + SR * 4])
    said = h.summary(h.stop())
    assert "didn't recognise the song" in said
    r = h.recap("who was that")
    assert r and r.startswith("I don't know.") and "couldn't hear" not in r and "by <band>" in r
    assert h.recap("what's for dinner") is None
    h.last_end -= hearing.RECAP_S + 1
    assert h.recap("who was that") is None                 # too long ago: the normal conversation answers


def test_who_was_that_names_the_recognised_song(tmp_path):
    h = hearing.Hearing(tmp_path)
    h.library.add("Aja", "Steely Dan", hearing.song_print(tune(4)), vid="aja")
    h.start("owner")
    mic = through_room(tune(4)[SR * 10: SR * 30])
    for i in range(0, len(mic), SR * 4):
        h.feed(mic[i: i + SR * 4])
    h.stop()
    assert h.recap("Who was that?") == "That was “Aja” by Steely Dan, I'm fairly sure."
    assert h.last.audio == []                              # the sound itself is let go at stop


def test_recap_words():
    for t in ("who was that", "Who was that band?", "what song was that", "did you recognize it",
              "who sang that", "any idea who that was", "What was that?"):
        assert hearing.asks_recap(t), t
    for t in ("who was at the door", "what time is it", "who is the loved one"):
        assert not hearing.asks_recap(t), t
