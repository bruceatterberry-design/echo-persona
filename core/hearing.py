"""Echo listens to music WITH the owner or the loved one through the room mic (2026-11-03, the owner: "truly listen with me").

Part 1 of the plan in STATUS.md: listen-with-me mode, room correction, recognising songs he has heard before.

* Audio comes in pieces (a few seconds, 22,050 Hz mono, raw: no browser echo cancelling or noise suppression) and is
  only held in memory while it is turned into numbers. Nothing is written to disk but those numbers.
* Pieces where someone is talking (a known voice scores high), or where Echo himself is speaking (the window doesn't
  send those), are skipped.
* Room correction: the speakers, the room and the mic colour the sound. Each time Echo hears through the mic a song he
  also has a clean print of (he put it on himself, or recognised it), he compares the two spectra band by band and
  updates a room curve (running average, per computer). Later pieces are equalised by the inverse curve and the
  quiet-room noise floor is subtracted before anything is measured. The curve gets better every session.
* Song prints: chroma (pitch classes; centred when matching, which makes it robust to EQ and echo) at
  ~0.19 s per frame + a 40-band mel spectrum per frame (for the room curve). Made from the clean audio whenever Echo
  listens to a song from YouTube (core/music.py), or from the mic when the owner tells him what a song is.
* Recognition: the last ~20-30 s of corrected mic chroma slides along every print; the best average similarity,
  compared with the next-best song, gives a confidence.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("echo.hearing")

SR = 22050
HOP = 4096                 # ~0.186 s per frame
N_MELS = 40
MATCH_MIN_S = 12.0         # need this much clean music before guessing
MATCH_WINDOW_S = 30.0      # the newest this-many seconds are matched
SPEECH_SCORE = 0.62        # a known voice at least this strong in a piece…
SPEECH_RISE = 0.10         # …and this much above the music's own level this session = someone talking: skip it
SESSION_MAX_S = 2 * 3600
RECAP_S = 15 * 60                # "who was that?" still answered this long after stop

# decided by scripts/bench_hearing.py on real songs + simulated rooms (see STATUS.md)
RECOGNISE_AT = 0.40        # best mean similarity (centred chroma): true 0.60+ (90%), wrong songs ≤ 0.27
MARGIN_AT = 0.10           # ahead of the next song by at least this


def _mel_db(y: np.ndarray, sr: int = SR) -> np.ndarray:
    import librosa
    m = librosa.feature.melspectrogram(y=y, sr=sr, n_fft=4096, hop_length=HOP, n_mels=N_MELS, fmin=60, fmax=10000)
    return librosa.power_to_db(m + 1e-10).astype(np.float32)          # (40, frames)


def _cens(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """Pitch-class frames (chroma CQT). Name kept from the first try (CENS): bench showed plain CQT chroma, centred
    in match(), separates songs far better (non-matches 0.25 max vs 0.97 for CENS)."""
    import librosa
    c = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)            # (12, frames)
    return c.astype(np.float32)


def song_print(y: np.ndarray, sr: int = SR) -> dict:
    """The numbers kept for a song (no audio): chroma frames, mel frames, tempo."""
    import librosa
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return {"chroma": _cens(y, sr), "mel": _mel_db(y, sr), "bpm": float(np.atleast_1d(tempo)[0])}


# ---------------------------------------------------------------------------------------------------- the library
class Library:
    """data/hearing/library.sqlite: one row per song print (title, artist, YouTube id, where it came from)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: list[dict] | None = None
        with self._db() as db:
            db.execute("""create table if not exists songs (id integer primary key, title text, artist text, vid text,
                          source text, bpm real, frames integer, chroma blob, mel blob, created real, heard integer
                          default 0, confirmed integer default 0)""")

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, title: str, artist: str, pr: dict, vid: str = "", source: str = "youtube") -> int:
        c, m = pr["chroma"].astype(np.float16), pr["mel"].astype(np.float16)
        with self._lock, self._db() as db:
            if vid:
                db.execute("delete from songs where vid=? and source=?", (vid, source))
            cur = db.execute("insert into songs (title, artist, vid, source, bpm, frames, chroma, mel, created) values "
                             "(?,?,?,?,?,?,?,?,?)", (title, artist, vid, source, float(pr.get("bpm") or 0), c.shape[1],
                                                     c.tobytes(), m.tobytes(), time.time()))
            self._cache = None
            return int(cur.lastrowid)

    def songs(self) -> list[dict]:
        with self._lock:
            if self._cache is None:
                with self._db() as db:
                    rows = db.execute("select id, title, artist, vid, source, bpm, frames, chroma, mel, heard, confirmed "
                                      "from songs").fetchall()
                self._cache = [{"id": r[0], "title": r[1], "artist": r[2], "vid": r[3], "source": r[4], "bpm": r[5],
                                "chroma": np.frombuffer(r[7], np.float16).reshape(12, r[6]).astype(np.float32),
                                "mel": np.frombuffer(r[8], np.float16).reshape(N_MELS, r[6]).astype(np.float32),
                                "heard": r[9], "confirmed": r[10]} for r in rows]
            return self._cache

    def by_vid(self, vid: str) -> dict | None:
        return next((s for s in self.songs() if vid and s["vid"] == vid), None)

    def bump(self, song_id: int, confirmed: bool = False) -> None:
        with self._lock, self._db() as db:
            db.execute("update songs set heard=heard+1, confirmed=confirmed+? where id=?", (int(confirmed), song_id))
            self._cache = None

    def count(self) -> int:
        return len(self.songs())


def label(song: dict) -> str:
    t, a = (song.get("title") or "").strip(), (song.get("artist") or "").strip()
    return f"“{t}” by {a}" if t and a else f"“{t}”" if t else "a song I know"


# ---------------------------------------------------------------------------------------------------- matching
def match(chroma: np.ndarray, songs: list[dict]) -> list[tuple[float, int, dict]]:
    """(best mean similarity, frame offset, song) per song, best first. chroma: (12, n) mic frames."""
    n = chroma.shape[1]
    out = []
    if n < 8:
        return out
    q = chroma - chroma.mean(axis=1, keepdims=True)       # centred: what's common to all music doesn't count
    q = q / (np.linalg.norm(q, axis=0, keepdims=True) + 1e-6)
    for s in songs:
        r = s["chroma"]
        if r.shape[1] < n:
            continue
        r = r - r.mean(axis=1, keepdims=True)
        rn = r / (np.linalg.norm(r, axis=0, keepdims=True) + 1e-6)
        sim = q.T @ rn                                  # (n, m) frame-to-frame cosine
        m = rn.shape[1]
        # mean along the diagonal for every offset (same speed: the same recording)
        idx = np.arange(n)
        offs = np.arange(m - n + 1)
        diag = sim[idx[None, :], offs[:, None] + idx[None, :]].mean(axis=1)
        k = int(np.argmax(diag))
        out.append((float(diag[k]), k, s))
    out.sort(key=lambda x: -x[0])
    return out


def verdict(ranked: list[tuple[float, int, dict]]) -> tuple[dict | None, float]:
    """(song, confidence 0-1) if recognised, else (None, how close the best one was)."""
    if not ranked:
        return None, 0.0
    best = ranked[0][0]
    nxt = ranked[1][0] if len(ranked) > 1 else 0.15   # one song in the library: compare with a typical non-match
    conf = float(np.clip((best - 0.25) / 0.4, 0, 1)) * float(np.clip((best - nxt) / (2 * MARGIN_AT), 0, 1))
    if best >= RECOGNISE_AT and best - nxt >= MARGIN_AT:
        return ranked[0][2], max(conf, 0.5)
    return None, conf


# ---------------------------------------------------------------------------------------------------- the room
class Room:
    """How this room + speakers + mic colour music: dB per mel band (shape only), a running average over calibrations,
    plus the quiet floor of the mic. data/hearing/room.json."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        self.curve = np.array(d.get("curve") or [0.0] * N_MELS, np.float32)
        self.count = int(d.get("count") or 0)
        self.history: list[float] = list(d.get("history") or [])   # how far off the room was before correction

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"curve": [round(float(x), 2) for x in self.curve], "count": self.count,
                                   "history": self.history[-200:], "updated": time.time()}))
        tmp.replace(self.path)

    def learn(self, mic_mel: np.ndarray, ref_mel: np.ndarray) -> float:
        """One calibration from aligned mic and clean frames (both (40, n) dB). Returns how far off (dB, mean abs)
        the room was before this calibration (after the current correction): falls as the curve improves."""
        loud = ref_mel.mean(axis=0) > np.percentile(ref_mel.mean(axis=0), 30)   # music, not the quiet bits
        if loud.sum() < 8:
            return -1.0
        diff = (mic_mel[:, loud] - ref_mel[:, loud]).mean(axis=1)
        diff -= diff.mean()                                                 # loudness doesn't matter, only shape
        before = float(np.abs(diff - self.curve).mean())
        w = 1.0 / (self.count + 1) if self.count < 10 else 0.1               # running average, then slowly following
        self.curve = ((1 - w) * self.curve + w * diff).astype(np.float32)
        self.count += 1
        self.history.append(round(before, 2))
        self.save()
        return before

    def gains(self, sr: int = SR, n_fft: int = 4096) -> np.ndarray:
        """Per STFT bin linear gain that undoes the curve (limited to ±12 dB so a bad guess can't wreck the sound)."""
        import librosa
        if not self.count:
            return np.ones(n_fft // 2 + 1, np.float32)
        centers = librosa.mel_frequencies(n_mels=N_MELS + 2, fmin=60, fmax=10000)[1:-1]
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        db = np.interp(freqs, centers, -np.clip(self.curve, -12, 12))
        return (10 ** (db / 20)).astype(np.float32)


def clean(y: np.ndarray, room: Room, floor: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """Room-equalised, noise-floor-reduced audio (mic piece in, cleaner piece out) + this piece's spectral floor."""
    import librosa
    S = librosa.stft(y, n_fft=4096, hop_length=1024)
    mag, ph = np.abs(S), np.angle(S)
    mag = mag * room.gains()[:, None]
    piece_floor = np.percentile(mag, 10, axis=1)
    fl = piece_floor if floor is None else np.minimum(0.9 * floor + 0.1 * piece_floor, piece_floor * 1.5)
    mag = np.maximum(mag - 1.5 * fl[:, None], 0.1 * mag)                   # gentle spectral subtraction
    out = librosa.istft(mag * np.exp(1j * ph), hop_length=1024, length=len(y))
    return out.astype(np.float32), fl


# ---------------------------------------------------------------------------------------------------- a session
@dataclass
class Session:
    by: str
    started: float = field(default_factory=time.time)
    audio: list = field(default_factory=list)       # cleaned pieces, newest MATCH_WINDOW_S only (memory, never disk)
    music_s: float = 0.0
    skipped_s: float = 0.0
    floor: np.ndarray | None = None
    song: dict | None = None
    confidence: float = 0.0
    recognised: list = field(default_factory=list)  # (time, song id, label, confidence)
    expected_vid: str = ""                          # the song Echo put on himself, if any
    last_match: float = 0.0
    calibrated: set = field(default_factory=set)
    stats: list = field(default_factory=list)       # per piece: bpm, loudness, brightness (corrected)
    new_song: str = ""                              # "just recognised" note for the window
    voice_levels: list = field(default_factory=list)  # voice-print score of music-only pieces (the baseline)
    since_split: float = 0.0                        # part 2: seconds of new music since the last split
    split_waiting: bool = False                     # logged "split waits" already for this window
    lyrics: list = field(default_factory=list)      # what I made out of the singing, piece by piece (memory only)
    parts: list = field(default_factory=list)       # stem_stats per split
    lookups: int = 0
    last_lookup: float = 0.0
    guess: dict | None = None                       # {"title","artist",...} from the words, waiting for a yes
    hit_titles: list = field(default_factory=list)  # every lyric search's result titles this session (votes add up)
    looked_at: int = 0                              # how many lyric pieces had been heard at the last lookup
    prints: dict = field(default_factory=lambda: {"singer": [], "guitarist": [], "drummer": []})   # part 3, newest
    sounds: dict = field(default_factory=dict)      # "guitar"/"drums": plain words from the newest split

    def recent(self) -> np.ndarray:
        return np.concatenate(self.audio) if self.audio else np.zeros(0, np.float32)


class Hearing:
    """The listen-with-me service: one session at a time."""

    def __init__(self, root: Path, speaker_scores=None, stt_sync=None, may_split=None, lookup=None,
                 separator: "Separator | None" = None, voice_embed=None):
        """stt_sync(audio 16 kHz) → words; may_split() → False while the owner/the loved one and I are talking (conversation comes
        first); lookup(phrase) → search result titles (the lyric words only). Any of them None = that part is off."""
        self.stt_sync, self.may_split, self.lookup = stt_sync, may_split, lookup
        self.separator = separator if separator is not None else (Separator() if stt_sync else None)
        self.dir = Path(root)
        self.library = Library(self.dir / "library.sqlite")
        self.musicians = Musicians(self.dir / "library.sqlite")   # part 3
        self.voice_embed = voice_embed                            # audio 16 kHz → unit voice print (voice/speaker.py)
        self.room = Room(self.dir / "room.json")
        self.speaker_scores = speaker_scores        # audio 16 kHz → {name: score}; None = no skipping
        self.session: Session | None = None
        self.last: Session | None = None            # the session that just ended ("who was that?" right after stop)
        self.last_end = 0.0
        self._lock = threading.Lock()

    # ---- start / stop
    def start(self, by: str, expected_vid: str = "") -> Session:
        with self._lock:
            self.session = Session(by=by, expected_vid=expected_vid)
            log.info("listen start by=%s splitter=%s songs known=%d", by, bool(self.separator and self.stt_sync),
                     len(self.library.songs()))
            return self.session

    def stop(self) -> Session | None:
        with self._lock:
            s, self.session = self.session, None
            if s is not None:
                self.last, self.last_end = s, time.time()
                log.info("listen stop: music %.0f s, skipped (talking) %.0f s, splits %d, sung pieces %d, "
                         "searches %d, song=%s guess=%s", s.music_s, s.skipped_s, len(s.parts), len(s.lyrics),
                         s.lookups, label(s.song) if s.song else None,
                         label(s.guess) if s.guess else None)
                self.last.audio = []                  # sound let go; only what I made of it stays (memory only)
            return s

    def warm(self) -> None:
        """2026-11-04: load the music splitter when listening starts, not at the first split (the owner's first try: it
        was still downloading when he said "stop listening", so no words were made out)."""
        if self.separator is not None and hasattr(self.separator, "load"):
            try:
                self.separator.load()
            except Exception:  # noqa: BLE001
                pass

    def release_now(self) -> bool:
        """Let the splitter go as soon as listening stops: it shares the GPU and memory with my voice."""
        return bool(self.separator and hasattr(self.separator, "release") and self.separator.release())

    def recent_session(self) -> Session | None:
        """The session running now, or the one that ended in the last RECAP_S."""
        if self.session is not None:
            return self.session
        if self.last is not None and time.time() - self.last_end <= RECAP_S:
            return self.last
        return None

    def recap(self, text: str) -> str | None:
        """"Who was that?" / "what song was that?" right after listening: answer from what I really heard (never
        "I couldn't hear it"). None = not that kind of question, or no recent session."""
        s = self.recent_session()
        if s is None or not asks_recap(text):
            return None
        names = []
        for _t, _i, lab, _c in s.recognised:
            if lab not in names:
                names.append(lab)
        if names:
            return f"That was {' and then '.join(names)}, I'm fairly sure."
        if s.guess:
            return f"From the words I made out, it might have been {label(s.guess)}. Is that right?"
        words = " ".join(s.lyrics).split()
        mins = s.music_s / 60
        heard = f"I heard about {max(1, round(mins))} minute{'s' if round(mins) > 1 else ''} of it"
        if words:
            bit = " ".join(words[:8])
            why = f", and I made out some of the words (\u201c{bit}\u201d), but they didn't lead me to the song"
        elif not s.parts:
            why = ", but I didn't get far enough to make out the words, so I couldn't work out the song"
        else:
            why = ", but I couldn't make out the words clearly enough to find the song"
        return (f"I don't know. {heard}{why}, and it isn't one I know by ear yet. If you tell me "
                f"\u201cthat was <song> by <band>\u201d, I'll remember it.")

    def active(self) -> bool:
        s = self.session
        if s and time.time() - s.started > SESSION_MAX_S:
            self.stop()
            return False
        return s is not None

    # ---- a piece of mic audio
    def is_speech(self, y: np.ndarray) -> bool:
        """Someone talking over the music. Real test (2026-11-03, Mac mini speakers + USB mic): Steely Dan alone scores
        0.76-0.79 against the owner's voice print, so a fixed bar can't work; talking = clearly ABOVE this session's own
        music level (median of the recent pieces) as well as above SPEECH_SCORE."""
        if not self.speaker_scores or len(y) < SR or self.session is None:
            return False
        try:
            import librosa
            sc = self.speaker_scores(librosa.resample(y, orig_sr=SR, target_sr=16000))
            top = max(sc.values() or [0])
        except Exception:  # noqa: BLE001
            return False
        seen = self.session.voice_levels
        base = float(np.median(seen[-8:])) if len(seen) >= 3 else None
        talking = base is not None and top >= SPEECH_SCORE and top >= base + SPEECH_RISE
        if not talking:
            seen.append(top)
        return talking

    def feed(self, y: np.ndarray) -> dict:
        """One piece (float32, SR). Returns what changed: {"skipped": bool, "song": label|None, "new": bool}."""
        s = self.session
        if s is None:
            return {"active": False}
        y = np.asarray(y, np.float32)
        secs = len(y) / SR
        if secs < 0.5 or float(np.sqrt(np.mean(y ** 2))) < 1e-4:    # silence / nothing from the mic
            return {"active": True, "skipped": True, "why": "quiet"}
        if self.is_speech(y):
            s.skipped_s += secs
            return {"active": True, "skipped": True, "why": "talking"}
        y = y / (np.max(np.abs(y)) + 1e-6) * 0.5                     # the mic level doesn't matter
        cy, s.floor = clean(y, self.room, s.floor)
        s.audio.append(cy)
        s.music_s += secs
        while sum(len(a) for a in s.audio) / SR > MATCH_WINDOW_S and len(s.audio) > 1:
            s.audio.pop(0)
        s.stats.append(self._stats(cy))
        s.since_split += secs
        out = {"active": True, "skipped": False}
        have = sum(len(a) for a in s.audio) / SR
        if have >= MATCH_MIN_S and time.time() - s.last_match > 4:
            s.last_match = time.time()
            out.update(self._recognise(s))
        if self.separator and self.stt_sync and s.since_split >= SEP_EVERY_S:
            if self.may_split is None or self.may_split():
                s.since_split, s.split_waiting = 0.0, False
                out.update(self._listen_closer(s))
            elif not s.split_waiting:                    # once per wait, not every piece
                s.split_waiting = True
                log.info("split waits: Echo is busy (conversation or low memory)")
        return out

    def _listen_closer(self, s: Session) -> dict:
        """Part 2: split the newest window into its parts, make out the sung words, and if I don't know the song yet,
        look a few of those words up."""
        import librosa
        y = s.recent()[-int(SEP_WINDOW_S * SR):]
        t0 = time.perf_counter()
        try:
            stems = self.separator.split(y)
        except Exception as e:  # noqa: BLE001
            log.warning("split failed: %s: %s", type(e).__name__, e)
            return {"split_error": type(e).__name__}
        st = stem_stats(stems)
        s.parts.append(st)
        out: dict = {"parts": st}
        self._musician_prints(s, stems, y, st)
        if st.get("vocals_share", 0) >= 0.12:   # somebody's singing
            v = stems["vocals"]
            v16 = librosa.resample(v / (np.max(np.abs(v)) + 1e-9) * 0.5, orig_sr=SR, target_sr=16000)
            try:
                words = (self.stt_sync(v16) or "").strip()
            except Exception:  # noqa: BLE001
                words = ""
            if words:
                s.lyrics.append(words)
                out["words"] = len(words.split())
        # the sung words themselves stay in memory only; the log gets counts
        log.info("split %.1f s: vocals %.0f%%, words %d", time.perf_counter() - t0,
                 100 * st.get("vocals_share", 0), out.get("words", 0))
        if (s.song is None and s.guess is None and self.lookup and s.lookups < LOOKUPS_PER_SESSION
                and len(s.lyrics) > s.looked_at and time.time() - s.last_lookup >= LOOKUP_GAP_S
                and sum(len(x.split()) for x in s.lyrics[s.looked_at:]) >= 8):
            phrases = lyric_phrases(s.lyrics[s.looked_at:])   # only words I haven't searched with yet
            if phrases:
                s.last_lookup, s.looked_at = time.time(), len(s.lyrics)
                for ph in phrases[: LOOKUPS_PER_SESSION - s.lookups]:
                    s.lookups += 1
                    try:
                        s.hit_titles.append(list(self.lookup(ph)))
                    except Exception as e:  # noqa: BLE001
                        log.warning("lyric search failed: %s", type(e).__name__)
                        s.hit_titles.append([])
                    # the phrase already left the Mac as a search, so it may be logged; titles are public results
                    log.info("lyric search %r -> %s", ph, s.hit_titles[-1])
                g = song_from_hits(s.hit_titles)   # votes from every search this session
                log.info("lyric votes (need %d from %d searches): %s -> %s", 3, 2,
                         hit_tally(s.hit_titles)[:3], label(g) if g else "no song yet")
                if g:
                    known = next((x for x in self.library.songs() if x["title"].lower() == g["title"].lower()
                                  and (x["artist"] or "").lower() == g["artist"].lower()), None)
                    s.guess = g
                    out["guess"] = label(g) + (" (I know it, but didn't recognise it by ear)" if known else "")
        return out

    def _stats(self, y: np.ndarray) -> dict:
        import librosa
        rms = float(np.sqrt(np.mean(y ** 2)))
        cen = float(np.median(librosa.feature.spectral_centroid(y=y, sr=SR, hop_length=2048)[0]))
        return {"at": time.time(), "rms": rms, "brightness_hz": round(cen)}

    def _recognise(self, s: Session) -> dict:
        y = s.recent()
        chroma = _cens(y)
        songs = self.library.songs()
        ranked = match(chroma, songs)
        song, conf = verdict(ranked)
        # the song Echo put on himself: a softer bar (he knows what should be playing)
        if song is None and s.expected_vid and ranked:
            exp = next((r for r in ranked if r[2]["vid"] == s.expected_vid), None)
            if exp and exp[0] >= RECOGNISE_AT - 0.1 and exp is ranked[0]:
                song, conf = exp[2], max(conf, 0.5)
        out = {"song": label(song) if song else None, "confidence": round(conf, 2), "new": False}
        if song and (s.song is None or s.song["id"] != song["id"]):
            s.song, out["new"] = song, True
            s.recognised.append((time.time(), song["id"], label(song), round(conf, 2)))
            log.info("recognised by ear: %s (confidence %.2f)", label(song), conf)
            self.library.bump(song["id"])
        s.song, s.confidence = (song, conf) if song else (s.song, s.confidence)
        if song and song["id"] not in s.calibrated:                  # learn the room from this one
            off = next(r[1] for r in ranked if r[2]["id"] == song["id"])
            raw_mel = _mel_db(y)
            n = min(raw_mel.shape[1], song["mel"].shape[1] - off)
            if n > 8:
                # the mic audio was already corrected by the current curve: add it back = how the raw room sounded
                before = self.room.learn(raw_mel[:, :n] + self.room.curve[:, None], song["mel"][:, off:off + n])
                s.calibrated.add(song["id"])
                out["room_off_db"] = round(before, 2)
        return out

    # ---- what Echo says
    def describe_now(self) -> str:
        s = self.session
        if s is None:
            return "I'm not listening through the microphone right now. Say \"listen with me\" and I will."
        if s.music_s < 6:
            return "I'm listening, but I haven't heard enough of the music yet. Give me a few more seconds."
        y = s.recent()
        from core import music
        a = music.analyse(y, SR)
        d = music.describe(a)
        feel = f"It's {d['pace']}, about {a['bpm']} beats a minute, {d['tone']}, with {d['dynamics']}"
        if s.parts:
            p = s.parts[-1]
            bits = []
            if p.get("vocals_share", 0) >= 0.12:
                bits.append("someone's singing")
            if p.get("drums_share", 0) >= 0.15:   # (the drums' own tempo was half the real one in the real test)
                bits.append("the drums are right up front" if p["drums_share"] >= 0.3 else "the drums keep it moving")
            if p.get("bass_share", 0) >= 0.2:
                bits.append("there's a strong bass line")
            if bits:
                feel += "; " + ", ".join(bits)
        feel += "."
        if s.sounds.get("guitar"):   # part 3: the players
            feel += f" The guitar has {s.sounds['guitar']}."
        if s.sounds.get("drums"):
            feel += f" The drums have {s.sounds['drums']}."
        for role in ROLES:
            if (c := self.clue(role)):
                feel += f" {c}."
        if s.song:
            return f"I think this is {label(s.song)}. {feel}"
        if s.guess:
            return f"From the words I could make out, it might be {label(s.guess)}. Is that right? {feel}"
        n = self.library.count()
        known = (f"I don't recognise it yet; I know {n} song{'s' if n != 1 else ''} by ear so far." if n else
                 "I don't know any songs by ear yet; each one I listen to teaches me.")
        return f"{feel} {known} If you tell me what it is, I'll remember how it sounds."

    def teach(self, title: str, artist: str) -> str:
        """the owner/the loved one: "this is Aja by Steely Dan" while listening → a print from what the mic heard."""
        s = self.session
        if s is None or s.music_s < MATCH_MIN_S:
            return "I need to hear a little more of it first. Tell me again in a few seconds."
        pr = song_print(s.recent())
        old = next((x for x in self.library.songs() if x["title"].lower() == title.lower()
                    and x["artist"].lower() == artist.lower()), None)
        if old:
            self.library.bump(old["id"], confirmed=True)
            return f"Thank you. I already know {label(old)}; I'll keep listening for it."
        sid = self.library.add(title, artist, pr, source="mic")
        if s.prints.get("singer"):
            self.musicians.song_singer(sid, self._mean(s.prints["singer"]))
        s.song = next((x for x in self.library.songs() if x["id"] == sid), None)
        return f"Thank you. I'll remember how {label(s.song)} sounds in this room."

    def _musician_prints(self, s: Session, stems: dict, mix: np.ndarray, st: dict) -> None:
        """Part 3: the numbers of who's playing, from this split (kept for this session; saved only when named)."""
        import librosa

        def keep(role, v):
            s.prints[role] = (s.prints[role] + [v])[-6:]
        try:
            if st.get("vocals_share", 0) >= 0.12 and self.voice_embed:
                v = stems["vocals"]
                v16 = librosa.resample(v / (np.max(np.abs(v)) + 1e-9) * 0.5, orig_sr=SR, target_sr=16000)
                if (vp := self.voice_embed(v16)) is not None:
                    keep("singer", np.asarray(vp, np.float32))
            if st.get("other_share", 0) >= 0.12:
                vec, nums = guitar_style(stems["other"])
                keep("guitarist", vec)
                s.sounds["guitar"] = guitar_words(nums)
            if st.get("drums_share", 0) >= 0.1:
                vec, nums = drum_style(stems["drums"], mix)
                keep("drummer", vec)
                s.sounds["drums"] = drum_words(nums)
        except Exception as e:  # noqa: BLE001
            s.sounds.setdefault("error", type(e).__name__)
        if s.song and s.prints["singer"]:   # the song remembers its singer's voice
            self.musicians.song_singer(s.song["id"], self._mean(s.prints["singer"]))

    @staticmethod
    def _mean(vs: list) -> np.ndarray:
        c = np.mean(np.stack(vs), axis=0)
        return (c / (np.linalg.norm(c) + 1e-9)).astype(np.float32)

    def clue(self, role: str) -> str | None:
        """Who it sounds like (a guess), or None."""
        s = self.session
        if s is None or not s.prints.get(role):
            return None
        vec = self._mean(s.prints[role])
        who, sim, lead = self.musicians.closest(role, vec)
        at, margin = (SINGER_AT, SINGER_MARGIN) if role == "singer" else (STYLE_AT, STYLE_MARGIN)
        if who and who["confirmed"] >= MIN_CONFIRMED and sim >= at and lead >= margin:
            part = {"singer": "The singer sounds like", "guitarist": "The guitar playing reminds me of",
                    "drummer": "The drumming reminds me of"}[role]
            return f"{part} {who['name']}, but that's my guess"
        if role == "singer":
            like = self.musicians.singer_like(vec, skip_song=s.song["id"] if s.song else None)
            if like and like[1] >= SINGER_AT + 0.05:
                return f"The voice reminds me of the singer in {like[0]}"
        return None

    def who(self, role: str) -> str:
        """"Who's singing / on guitar / on drums?" while listening."""
        s = self.session
        if s is None or not s.prints.get(role):
            what = {"singer": "the singing", "guitarist": "the guitar", "drummer": "the drums"}[role]
            return f"I haven't picked out {what} clearly enough yet. Give me a little longer."
        c = self.clue(role)
        if c:
            return c + ". Am I right?"
        sound = s.sounds.get({"singer": "", "guitarist": "guitar", "drummer": "drums"}[role])
        known = len([p for p in self.musicians.all(role) if p["confirmed"] >= MIN_CONFIRMED])
        base = f"I can't tell who it is yet{f'; it has {sound}' if sound else ''}."
        return (base + " If you tell me, I'll learn it" + (" — I need to hear someone confirmed twice before I guess."
                                                             if not known else "."))

    def teach_who(self, name: str, role: str) -> str:
        """"That's Donald Fagen singing": this session's prints for that part go to that person."""
        s = self.session
        if s is None or not s.prints.get(role):
            what = {"singer": "the singing", "guitarist": "the guitar", "drummer": "the drums"}[role]
            return f"I haven't picked out {what} clearly yet, so I can't learn it this time. Tell me again in a bit."
        band = (s.song or {}).get("artist") or ""
        n = self.musicians.learn(name, role, band, s.prints[role])
        part = {"singer": "voice", "guitarist": "guitar playing", "drummer": "drumming"}[role]
        more = (" Once more on another song and I'll start recognising it." if n < MIN_CONFIRMED else
                f" That's {n} times now; I'll listen for it.")
        return f"Thank you. I'm learning {name}'s {part}.{more}"

    def confirm_guess(self) -> str | None:
        """the owner/the loved one said yes to my guess from the words: learn it by ear."""
        s = self.session
        if s is None or not s.guess:
            return None
        g, s.guess = s.guess, None
        return self.teach(g["title"], g["artist"])

    def release_idle(self) -> bool:
        return bool(self.separator and self.separator.release_if_idle())

    def summary(self, s: Session) -> str:
        mins = max(1, round(s.music_s / 60))
        names = []
        for _t, _i, lab, _c in s.recognised:
            if lab not in names:
                names.append(lab)
        heard = (" I recognised " + ", ".join(names) + "." if names else
                 f" From the words, it might be {label(s.guess)}." if s.guess else
                 " I didn't recognise the song this time.")
        room = (f" My ear for this room is getting better: I've tuned it {self.room.count} time"
                f"{'s' if self.room.count != 1 else ''}." if s.calibrated else "")
        return f"Thanks for listening with me. That was about {mins} minute{'s' if mins != 1 else ''} of music.{heard}{room}"


# ---------------------------------------------------------------------------------------------------- the words
import re  # noqa: E402

_LEAD = (r"^(?:(?:hey|hi|okay|ok|so|well)[,\s]+)*(?:(?:echo|ecco|eco)[,.]?\s+)?(?:(?:can|could|will|would)\s+you\s+|"
         r"please\s+|let'?s\s+|i\s+(?:want|need|would\s+like)\s+you\s+to\s+|come\s+(?:and\s+)?|why\s+don'?t\s+you\s+)*")
_START = re.compile(_LEAD + r"(?:start\s+)?listen(?:ing)?\s+(?:(?:along\s+)?with\s+me|(?:to|with)\s+(?:this|the\s+music|"
                    r"the\s+radio|the\s+record|my\s+record|the\s+stereo|what'?s\s+playing|this\s+song|this\s+one|the\s+song)"
                    r"(?:\s+(?:along\s+)?with\s+me)?|through\s+the\s+(?:mic|microphone))(?:\s+(?:please|for\s+a\s+while|now))?\W*$",
                    re.I)
_HEAR_Q = re.compile(r"\b(?:what\s+do\s+you\s+hear|can\s+you\s+hear\s+(?:it|this|the\s+music)|what\s+(?:is|'s)\s+this|"
                     r"do\s+you\s+(?:know|recognize|recognise)\s+(?:it|this|this\s+(?:one|song))|how\s+does\s+(?:it|this)\s+sound)\b",
                     re.I)
_STOP = re.compile(r"\b(?:stop|quit|you\s+can\s+stop|that'?s\s+enough)\s+listening(?:\s+(?:with\s+me|to\s+(?:this|the\s+music|it)))?\b|"
                   r"\bthat'?s\s+enough\s+music\b|\bwe'?re\s+done\s+listening\b", re.I)
_TEACH = re.compile(r"^(?:(?:no|yes|yeah)[,\s]+)?(?:this|that|it)(?:'s|\s+is|\s+was)\s+[\"“]?(?P<t>.{2,60}?)[\"”]?\s+by\s+"
                    r"(?P<a>.{2,40}?)\W*$", re.I)


def asks_start(text: str) -> bool:
    return bool(_START.match((text or "").strip().replace("’", "'")))


_RECAP = re.compile(r"\b(?:who\s+(?:was|is)\s+(?:that|this|it|singing|the\s+(?:band|singer|artist))|"
                    r"who\s+(?:sang|played|sings)\s+(?:that|this|it)|what\s+(?:song|band|track|music)\s+(?:was|is)\s+"
                    r"(?:that|this|it)|what\s+was\s+(?:that|it|the\s+(?:song|band|track))|"
                    r"(?:do|did)\s+you\s+(?:know|recogni[sz]e)\s+(?:that|it|this|the\s+(?:song|band|track))|"
                    r"(?:do|did)\s+you\s+(?:figure|work)\s+(?:it|that)\s+out|any\s+idea\s+(?:who|what))\b", re.I)


def asks_recap(text: str) -> bool:
    return bool(_RECAP.search((text or "").replace("\u2019", "'")))


def asks_stop(text: str) -> bool:
    return bool(_STOP.search(text or ""))


def asks_what(text: str) -> bool:
    return bool(_HEAR_Q.search(text or ""))


def teaching(text: str) -> tuple[str, str] | None:
    m = _TEACH.match((text or "").strip().replace("’", "'"))
    if not m:
        return None
    t, a = m.group("t").strip(" ,"), m.group("a").strip(" ,")
    if re.match(r"(?:a|the)\s+(?:song|band)$", t, re.I) or t.lower() in ("me", "you", "it"):
        return None
    if re.fullmatch(r"(?:the\s+way|far|now|me|you|us|them|him|her|then|the\s+time|a\s+mile|default|design)", a, re.I):
        return None   # "that was great by the way"
    if re.fullmatch(r"(?:great|good|nice|fun|lovely|beautiful|awesome|amazing|cool|wonderful|fine|ok|okay|done)", t, re.I):
        return None
    return t, a


def title_artist(info) -> tuple[str, str]:
    """From YouTube details: ("Aja", "Steely Dan")."""
    if getattr(info, "track", "") and getattr(info, "artist", ""):
        return info.track, info.artist
    lab = info.label()
    m = re.match(r"^“(.+)” by (.+)$", lab)
    return (m.group(1), m.group(2)) if m else (lab.strip("“”"), "")


# ================================================================================================ part 2 (2026-11-04)
# Splitting the music (Demucs htdemucs, 84 MB, MIT; the owner OK'd the download) into vocals / drums / bass / other, the
# sung words from the vocals (Parakeet, same as speech), and finding an unknown song online from a few of those words
# (the owner: "lyric words only": the words go to DuckDuckGo, never audio).
SEP_EVERY_S = 20.0         # split the newest window after this much new music
SEP_WINDOW_S = 24.0
LOOKUP_GAP_S = 15.0        # splits come every 20 s; real test (Peg): the clearer verse was in the 2nd split
LOOKUPS_PER_SESSION = 9    # searches (3 per lookup), so DuckDuckGo isn't pestered
_STOPWORDS = set("""a an the and or but so to of in on at for with you your i me my we our it its is are was were be
this that oh ooh yeah la na hey uh um baby""".split())


class Separator:
    """Demucs, loaded on first use, let go after 10 idle minutes. Apple GPU if it's there (1.9 s per 30 s of music on
    the M4), else CPU (6 s)."""

    def __init__(self, idle_s: float = 600):
        self.model = None
        self.device = None
        self.last = 0.0
        self.idle_s = idle_s
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self.model is None:
                self._load()
            self.last = time.time()

    def release(self) -> bool:
        with self._lock:
            if self.model is None:
                return False
            self.model = None
            try:
                import torch
                torch.mps.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            return True

    def _load(self):
        import torch
        from demucs.pretrained import get_model
        m = get_model("htdemucs")
        m.eval()
        self.model = m
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"

    def split(self, y: np.ndarray, sr: int = SR) -> dict[str, np.ndarray]:
        """{'vocals','drums','bass','other'} mono at `sr`."""
        import librosa
        import torch
        from demucs.apply import apply_model
        with self._lock:
            if self.model is None:
                self._load()
            self.last = time.time()
            x = librosa.resample(np.asarray(y, np.float32), orig_sr=sr, target_sr=self.model.samplerate)
            t = torch.from_numpy(x).float()[None].repeat(2, 1)[None]
            try:
                with torch.no_grad():
                    out = apply_model(self.model, t, device=self.device, shifts=0, split=True, overlap=0.1,
                                      progress=False)[0]
            except Exception:  # noqa: BLE001  (GPU busy / out of memory: the CPU does it)
                self.device = "cpu"
                with torch.no_grad():
                    out = apply_model(self.model, t, device="cpu", shifts=0, split=True, overlap=0.1, progress=False)[0]
            return {name: librosa.resample(out[i].mean(0).numpy(), orig_sr=self.model.samplerate, target_sr=sr)
                    for i, name in enumerate(self.model.sources)}

    def release_if_idle(self) -> bool:
        with self._lock:
            if self.model is not None and time.time() - self.last > self.idle_s:
                self.model = None
                try:
                    import torch
                    torch.mps.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
                return True
        return False


def stem_stats(stems: dict[str, np.ndarray], sr: int = SR) -> dict:
    """How much of each part there is, and the drums' own tempo (cleaner than the full mix's)."""
    import librosa
    rms = {k: float(np.sqrt(np.mean(v ** 2))) for k, v in stems.items()}
    total = sum(rms.values()) or 1e-9
    out = {f"{k}_share": round(v / total, 3) for k, v in rms.items()}
    try:
        tempo, _ = librosa.beat.beat_track(y=stems["drums"], sr=sr)
        out["drums_bpm"] = round(float(np.atleast_1d(tempo)[0]))
    except Exception:  # noqa: BLE001
        pass
    return out


def lyric_phrases(chunks: list[str], n: int = 3, size: int = 5) -> list[str]:
    """Short searchable pieces of what was sung: 5-word runs with the most real words, preferring words heard more than
    once (the chorus). Different pieces, not overlapping."""
    words_by_chunk = [re.findall(r"[a-z']+", c.lower()) for c in chunks if c]
    counts: dict[str, int] = {}
    for ws in words_by_chunk:
        for w in set(ws):
            counts[w] = counts.get(w, 0) + 1
    cands = []
    for ws in words_by_chunk:
        for i in range(0, max(0, len(ws) - size) + 1):
            win = ws[i:i + size]
            if len(win) < size:
                continue
            real = [w for w in win if w not in _STOPWORDS and len(w) > 2]
            if len(real) < 2:
                continue
            cands.append((len(real) + 0.5 * sum(counts[w] > 1 for w in real), " ".join(win)))
    cands.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _sc, ph in cands:
        if all(len(set(ph.split()) & set(o.split())) <= 2 for o in out):
            out.append(ph)
        if len(out) >= n:
            break
    return out


_HIT_TITLE = re.compile(r"^(?P<a>[^|\[\]]{2,60}?)\s+[-–—]\s+(?P<t>[^|]{1,80}?)\s+Lyrics\b", re.I)


def song_from_hits(titles_by_query: list[list[str]], min_votes: int = 3, min_queries: int = 2) -> dict | None:
    """Search result titles like "Steely Dan - Do It Again Lyrics | Genius Lyrics" → the song most results agree on
    (several results, from more than one search). None if they don't agree."""
    votes = _votes(titles_by_query)
    if not votes:
        return None
    best = max(votes.values(), key=lambda v: (len(v["queries"]), v["votes"]))
    if best["votes"] >= min_votes and len(best["queries"]) >= min(min_queries, len(titles_by_query)):
        return {"title": best["title"], "artist": best["artist"], "votes": best["votes"],
                "queries": len(best["queries"])}
    return None


def hit_tally(titles_by_query: list[list[str]]) -> list[str]:
    """For the log: the songs the searches pointed at, most agreed first ("Artist - Title: 2 results / 1 search")."""
    vs = sorted(_votes(titles_by_query).values(), key=lambda v: (len(v["queries"]), v["votes"]), reverse=True)
    return [f"{v['artist']} - {v['title']}: {v['votes']} results / {len(v['queries'])} searches" for v in vs]


def _votes(titles_by_query: list[list[str]]) -> dict[tuple[str, str], dict]:
    votes: dict[tuple[str, str], dict] = {}
    for qi, titles in enumerate(titles_by_query):
        for t in titles:
            m = _HIT_TITLE.match(t.strip())
            if not m or "youtube" in t.lower():
                continue
            a, ti = m.group("a").strip(), m.group("t").strip()
            key = (a.lower(), ti.lower())
            v = votes.setdefault(key, {"artist": a, "title": ti, "votes": 0, "queries": set()})
            v["votes"] += 1
            v["queries"].add(qi)
    return votes


# ================================================================================================ part 3 (2026-11-04)
# Musician clues. Nobody can be named by ear alone, so Echo learns them the way he learned faces and voices: the owner or
# the loved one tells him ("that's Donald Fagen singing", "the drummer is Jeff Porcaro"), and he keeps the numbers from that
# moment (a voice print of the separated singing; style numbers for the guitar and the drums). Later he says who it
# SOUNDS like, only once a person has been confirmed at least twice, and always as a guess. Without names he can
# still say "the voice reminds me of the singer in “Peg”" (songs remember their singer's voice print).
# NOTE: the singer-print benchmark (scripts/bench_singers.py) was stopped by the owner before results; the bars below are
# deliberately cautious and should be checked against real confirmations (part 4's accuracy log).
SINGER_AT, SINGER_MARGIN = 0.60, 0.08
STYLE_AT, STYLE_MARGIN = 0.90, 0.04
MIN_CONFIRMED = 2
ROLES = ("singer", "guitarist", "drummer")


def guitar_style(other: np.ndarray, sr: int = SR) -> tuple[np.ndarray, dict]:
    """The 'other' part (guitars, keys): a style vector + plain numbers."""
    import librosa
    y = other / (np.max(np.abs(other)) + 1e-9)
    cen = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr)[0]))
    flat = float(np.median(librosa.feature.spectral_flatness(y=y)[0]))
    S = np.abs(librosa.stft(y, n_fft=2048))
    f = librosa.fft_frequencies(sr=sr, n_fft=2048)
    hi = float(S[f > 2500].sum() / (S.sum() + 1e-9))              # grit / distortion lives up here
    rms = librosa.feature.rms(y=y)[0]
    sustain = float((rms > 0.3 * rms.max()).mean()) if rms.size else 0.0
    dens = float(len(librosa.onset.onset_detect(y=y, sr=sr)) / max(len(y) / sr, 1))
    mf = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13).mean(axis=1)
    nums = {"brightness_hz": round(cen), "flatness": round(flat, 4), "high_share": round(hi, 3),
            "sustain": round(sustain, 2), "notes_per_s": round(dens, 2)}
    vec = np.concatenate([mf / 100.0, [cen / 4000, flat * 20, hi * 4, sustain, dens / 8]]).astype(np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9), nums


def guitar_words(n: dict) -> str:
    tone = "gritty, distorted" if n["high_share"] > 0.22 or n["flatness"] > 0.02 else "clean"
    shade = "bright" if n["brightness_hz"] > 2400 else "warm" if n["brightness_hz"] < 1400 else "smooth"
    play = "busy, lots of notes" if n["notes_per_s"] > 4 else "long held notes" if n["sustain"] > 0.6 else "a relaxed part"
    return f"a {tone}, {shade} sound, {play}"


def drum_style(drums: np.ndarray, mix: np.ndarray, sr: int = SR) -> tuple[np.ndarray, dict]:
    """The drums: busy or sparse, straight or swung (where the in-between hits land between beats), how hard."""
    import librosa
    y = drums / (np.max(np.abs(drums)) + 1e-9)
    _t, beats = librosa.beat.beat_track(y=mix, sr=sr, units="time")
    on = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    env = librosa.onset.onset_strength(y=y, sr=sr)
    phases = []
    for t in on:
        k = np.searchsorted(beats, t) - 1
        if 0 <= k < len(beats) - 1:
            phases.append((t - beats[k]) / max(beats[k + 1] - beats[k], 1e-3))
    ph = np.array(phases)
    hist = np.histogram(ph, bins=8, range=(0, 1))[0].astype(np.float32) if len(ph) else np.zeros(8, np.float32)
    hist = hist / (hist.sum() + 1e-9)
    mid = ph[(ph > 0.35) & (ph < 0.8)] if len(ph) else ph
    swing = float(np.median(mid)) if len(mid) >= 4 else 0.5
    dens = float(len(on) / max(len(y) / sr, 1))
    punch = float(np.std(env) / (np.mean(env) + 1e-9))
    cen = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr)[0]))
    nums = {"hits_per_s": round(dens, 2), "swing": round(swing, 2), "punch": round(punch, 2), "brightness_hz": round(cen)}
    vec = np.concatenate([hist, [dens / 8, (swing - 0.5) * 4, punch / 3, cen / 5000]]).astype(np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9), nums


def drum_words(n: dict) -> str:
    feel = "a shuffle, swung feel" if n["swing"] >= 0.6 else "a straight feel"
    busy = "busy, with lots of fills" if n["hits_per_s"] > 6 else "sparse and laid back" if n["hits_per_s"] < 3 else "steady"
    hit = "hit hard" if n["punch"] > 1.6 else "played lightly" if n["punch"] < 0.9 else ""
    return ", ".join(x for x in (feel, busy, hit) if x)


class Musicians:
    """data/hearing/library.sqlite table musicians: name, role, band, prints (newest 30), how often confirmed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.execute("""create table if not exists musicians (id integer primary key, name text, role text, band text,
                          dim integer, prints blob, confirmed integer default 0, updated real)""")
            cols = [r[1] for r in db.execute("pragma table_info(songs)")]
            if "singer" not in cols:   # a song remembers its singer's voice print (no name needed)
                db.execute("alter table songs add column singer blob")

    def all(self, role: str | None = None) -> list[dict]:
        with self._lock, sqlite3.connect(self.path) as db:
            rows = db.execute("select id, name, role, band, dim, prints, confirmed from musicians" +
                              (" where role=?" if role else ""), ((role,) if role else ())).fetchall()
        out = []
        for r in rows:
            p = np.frombuffer(r[5], np.float16).reshape(-1, r[4]).astype(np.float32)
            c = p.mean(axis=0)
            out.append({"id": r[0], "name": r[1], "role": r[2], "band": r[3], "prints": p,
                        "centroid": c / (np.linalg.norm(c) + 1e-9), "confirmed": r[6]})
        return out

    def learn(self, name: str, role: str, band: str, prints: list[np.ndarray]) -> int:
        """Adds this session's prints to that person (made if new). Returns how many times confirmed now."""
        prints = [p for p in prints if p is not None]
        if not prints:
            return -1
        new = np.stack(prints).astype(np.float32)
        with self._lock, sqlite3.connect(self.path) as db:
            row = db.execute("select id, dim, prints, confirmed from musicians where lower(name)=? and role=?",
                             (name.lower(), role)).fetchone()
            if row and row[1] == new.shape[1]:
                old = np.frombuffer(row[2], np.float16).reshape(-1, row[1]).astype(np.float32)
                allp = np.concatenate([old, new])[-30:]
                db.execute("update musicians set prints=?, confirmed=confirmed+1, updated=?, band=coalesce(nullif(?, ''), band) "
                           "where id=?", (allp.astype(np.float16).tobytes(), time.time(), band, row[0]))
                return row[3] + 1
            db.execute("insert into musicians (name, role, band, dim, prints, confirmed, updated) values (?,?,?,?,?,1,?)",
                       (name, role, band, new.shape[1], new.astype(np.float16).tobytes(), time.time()))
            return 1

    def closest(self, role: str, vec: np.ndarray) -> tuple[dict | None, float, float]:
        """(person, similarity, lead over the next person); None if nobody of that role is known."""
        ppl = [p for p in self.all(role) if p["prints"].shape[1] == len(vec)]
        if not ppl:
            return None, 0.0, 0.0
        sims = sorted(((float(p["centroid"] @ vec), p) for p in ppl), key=lambda x: -x[0])
        nxt = sims[1][0] if len(sims) > 1 else 0.0
        return sims[0][1], sims[0][0], sims[0][0] - nxt

    def song_singer(self, song_id: int, vec: np.ndarray) -> None:
        with self._lock, sqlite3.connect(self.path) as db:
            db.execute("update songs set singer=? where id=?", (vec.astype(np.float16).tobytes(), song_id))

    def singer_like(self, vec: np.ndarray, skip_song: int | None = None) -> tuple[str, float] | None:
        """The song whose singer sounds most like this voice: (label, similarity)."""
        with self._lock, sqlite3.connect(self.path) as db:
            rows = db.execute("select id, title, artist, singer from songs where singer is not null").fetchall()
        best = None
        for sid, t, a, b in rows:
            if sid == skip_song:
                continue
            v = np.frombuffer(b, np.float16).astype(np.float32)
            if len(v) != len(vec):
                continue
            sim = float(v @ vec / (np.linalg.norm(v) * np.linalg.norm(vec) + 1e-9))
            if best is None or sim > best[1]:
                best = (label({"title": t, "artist": a}), sim)
        return best


_WHO_Q = re.compile(r"\bwho(?:'s|\s+is)?\s+(?:(?P<s>singing|the\s+singer|sings)|(?:on|playing(?:\s+the)?|the)\s+"
                    r"(?P<g>guitars?|guitarist|lead\s+guitar)|(?:on|playing(?:\s+the)?|the)\s+(?P<d>drums?|drummer))\b",
                    re.I)
_NAME = r"(?P<n>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})"
_TEACH_WHO = [
    (re.compile(r"^(?:that'?s|this\s+is|it'?s)\s+" + _NAME + r"\s+(?:singing|on\s+vocals)\b"), "singer"),
    (re.compile(r"^(?:the\s+(?:lead\s+)?(?:singer|vocalist|voice)\s+is|" + r"that\s+voice\s+is)\s+" + _NAME), "singer"),
    (re.compile(r"^" + _NAME + r"\s+(?:is\s+)?(?:singing|sings\s+(?:this|it|lead))\b"), "singer"),
    (re.compile(r"^(?:that'?s|this\s+is|it'?s)\s+" + _NAME + r"\s+on\s+(?:lead\s+)?guitar\b"), "guitarist"),
    (re.compile(r"^the\s+(?:lead\s+)?guitar(?:ist|\s+player)?\s+(?:is|was)\s+" + _NAME), "guitarist"),
    (re.compile(r"^" + _NAME + r"\s+(?:is\s+)?(?:playing|plays|played)\s+(?:the\s+)?(?:lead\s+)?guitar\b"), "guitarist"),
    (re.compile(r"^(?:that'?s|this\s+is|it'?s)\s+" + _NAME + r"\s+on\s+(?:the\s+)?drums\b"), "drummer"),
    (re.compile(r"^" + _NAME + r"\s+on\s+(?:lead\s+)?guitar\b"), "guitarist"),
    (re.compile(r"^" + _NAME + r"\s+on\s+(?:the\s+)?drums\b"), "drummer"),
    (re.compile(r"^" + _NAME + r"\s+on\s+(?:lead\s+)?vocals\b"), "singer"),
    (re.compile(r"^the\s+drummer\s+(?:is|was)\s+" + _NAME), "drummer"),
    (re.compile(r"^" + _NAME + r"\s+(?:is\s+)?(?:playing|plays|played)\s+(?:the\s+)?drums\b"), "drummer"),
]


def asks_who(text: str) -> str | None:
    m = _WHO_Q.search(text or "")
    if not m:
        return None
    return "singer" if m.group("s") else "guitarist" if m.group("g") else "drummer"


def teaching_who(text: str) -> tuple[str, str] | None:
    """"That's Donald Fagen singing" → ("Donald Fagen", "singer"). Names need capitals (speech recognition writes them)."""
    t = re.sub(r"^(?:(?:no|yes|yeah|well|oh)[,\s]+)+", "", (text or "").strip().replace("’", "'"), flags=re.I)
    t = re.sub(r"[.!?]+$", "", t)
    for rx, role in _TEACH_WHO:
        m = rx.match(t[0].lower() + t[1:] if t[:1].isupper() and not rx.pattern.startswith("^" + _NAME) else t)
        if m:
            name = m.group("n").strip()
            if name.lower() not in ("he", "she", "it", "they", "that", "this", "the", "you", "i", "me"):
                return name, role
    return None
