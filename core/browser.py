"""Open web pages when asked: "Hey Echo, open the Weather Channel" / "go to weather dot com".

Recognized before the LLM sees the message, so an address is only ever one that was said out loud or saved as a
shortcut in config/browser.json; the model never makes one up. Only http/https pages open (in your normal Chrome),
or one of Echo's own scripts named in that file. Anything that isn't clearly a request to open something goes to the
conversation as usual.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from core.config import ROOT

CONFIG = ROOT / "config" / "browser.json"

# Optional: which signed-in Google account YouTube uses when Echo plays music (?authuser=…). Blank = the browser's default.
LOVED_ONE_ACCOUNT = ""
OWNER_ACCOUNT = ""

# "can you please open up chrome and go to", "take me to", "pull up a chrome window with"…
LEAD = r"^(?:(?:(?:hey|hi|okay|ok|so|um|uh)\s+)*(?:echo|ekko|eko|ecco|eco|ecko|nako|neko|necko)[,.]?\s+)?(?:(?:can|could|would|will)\s+you\s+|please\s+|i\s+(?:want|need)\s+(?:you\s+)?to\s+)*"
VERB = r"(?:open(?:\s+up)?|go\s+to|take\s+me\s+to|pull\s+up|bring\s+up|show\s+me|launch|start(?:\s+up)?|load)"
CHROME = (r"(?:(?:a\s+(?:new\s+)?)?(?:google\s+)?(?:chrome|browser|web\s+browser)(?:\s+window)?"
          r"(?:\s+(?:to|at|with|on|and\s+(?:go\s+to|open|show(?:\s+me)?|pull\s+up|load)))?\s*)")
REQUEST_RE = re.compile(LEAD + VERB + r"\s+(?P<chrome>" + CHROME + r")?(?P<target>.*)$", re.I)
TRAIL_RE = re.compile(r"(?:\s+(?:please|for\s+me|for\s+us|for\s+you|right\s+now|right\s+away|now|together|in\s+(?:google\s+)?chrome|"
                      r"on\s+the\s+computer|thanks?(?:\s+you)?))+$", re.I)
SPOKEN = [(r"\s+dot\s+", "."), (r"\s*\bslash\b\s*", "/"), (r"\s*\bdash\b\s*", "-"), (r"\bdouble\s*u\b", "w")]
TLDS = r"(?:com|org|net|edu|gov|us|io|co|info|tv|me|ai|app|dev|uk|ca)"
DOMAIN_RE = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+" + TLDS + r"(?:[/?#]\S*)?$", re.I)


@dataclass
class Action:
    say: str                 # what Echo answers
    url: str | None = None   # page to open
    script: str | None = None
    play: str | None = None  # YouTube: find the top video for this and open it (url = the search page, the fallback)
    opened: str | None = None   # set by run(): the address really opened
    video: bool = False         # a video (not music): played, but Echo doesn't "listen along"
    random: bool = False        # "a random cat video": one of the top results, not always the first
    expression: str = "smiling"
    answers: dict[str, str] | None = None   # after a question: short replies ("mine") -> shortcut to open
    close: str | None = None    # 2026-11-02: close Chrome tabs showing this site (host, e.g. "weather.com")
    exclude: list[str] | None = None   # 2026-11-03: "a different song": video ids not to play again
    avoid: list[str] | None = None     # … and song titles not to pick ("instead of Do It Again")


# 2026-11-02 (the owner's log): "I think that we could uh open the weather channel and see what the weather's going to be
# like" wasn't recognised. Hedges in front and a purpose clause behind are now taken off before matching.
_UHM = re.compile(r"\b(?:uh+|um+|er+|erm|hmm+)\b", re.I)
# 2026-11-03 (the owner's log): "Go ahead and open the weather channel" / "Go ahead and open Steely Dan" weren't commands,
# so the LLM said "I'm opening it" and nothing opened. "go ahead and" in front is now taken off too.
_INTERJ = re.compile(r"^(?:(?:well|so|okay|ok|oh|yeah|yes|yep|sure|alright|all\s+right|hey|hi|good|great|cool|nice|thanks|"
                     r"thank\s+you|now|and|then|go\s+ahead(?:\s+and)?|please\s+go\s+ahead(?:\s+and)?)\s+)+", re.I)
_HEDGE = re.compile(r"^(?:i\s+think\s+(?:that\s+)?|maybe\s+|perhaps\s+)?"
                    r"(?:we\s+(?:could|should|can|might)\s+|let'?s\s+|how\s+about\s+(?:we\s+)?|why\s+don'?t\s+(?:we|you)\s+|"
                    r"(?:can|could|shall)\s+we\s+|i'?d\s+like\s+(?:you\s+)?to\s+|i\s+would\s+like\s+(?:you\s+)?to\s+)", re.I)
_PURPOSE = re.compile(r"\s+(?:(?:and|so|to)\s+(?:that\s+)?(?:we\s+can\s+|i\s+can\s+|you\s+can\s+|let'?s\s+)?"
                      r"(?:see|check|find\s+out|look|watch|hear|listen|learn|know|enjoy)\b|so\s+(?:that\s+)?(?:we|i|you)\s+"
                      r"(?:can|could|may|might)\b).*$", re.I)


def _clean(s: str) -> str:
    s = s.strip().lower().replace("’", "'")
    s = re.sub(r"[\"“”!?,]+", "", s).rstrip(".").strip()
    s = re.sub(r"\s+", " ", _UHM.sub(" ", s)).strip()
    s = _HEDGE.sub("", _INTERJ.sub("", s))
    s = TRAIL_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _key(s: str) -> str:   # "The  Weather Channel" == "weather channel"
    s = re.sub(r"^(?:the|my|our)\s+", "", _clean(s).replace("'", ""))
    return re.sub(r"\s+(?:page|website|site|web\s*site|channel\s+website|account)$", "", s)


def load_shortcuts(path: Path = CONFIG) -> list[dict]:
    try:
        return json.loads(path.read_text()).get("shortcuts", [])
    except (OSError, ValueError):
        return []


def spoken_url(target: str) -> str | None:
    """"weather dot com slash maps" -> https://weather.com/maps; None unless it really looks like an address."""
    t = f" {target} "
    for pat, rep in SPOKEN:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r"\s+", "", t.strip()).rstrip(".")
    if not DOMAIN_RE.match(t):
        return None
    if not re.match(r"https?://", t, re.I):
        t = "https://" + t
    parts = urlsplit(t)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()).geturl()   # the path keeps its case


def parse(text: str, shortcuts: list[dict] | None = None) -> Action | None:
    """An Action if the message asks to open something, else None (a normal conversation turn). Each sentence is tried,
    the last first ("I'd love that. Let's open the weather channel.")."""
    for sent in [x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x.strip()][::-1] or [text or ""]:
        if (a := _parse_one(sent, shortcuts)):
            return a
    return None


def _parse_one(text: str, shortcuts: list[dict] | None = None) -> Action | None:
    m = REQUEST_RE.match(_PURPOSE.sub("", _clean(text)))
    if not m:
        return None
    target, said_chrome = TRAIL_RE.sub("", m.group("target")).strip(), bool(m.group("chrome"))
    typed = _UHM.sub(" ", text.strip().replace("’", "'").rstrip(".!?\"” "))
    raw = REQUEST_RE.match(TRAIL_RE.sub("", _HEDGE.sub("", _INTERJ.sub("", re.sub(r"\s+", " ", typed).strip()))))   # as typed
    shortcuts = load_shortcuts() if shortcuts is None else shortcuts
    if target:
        k = _key(target)
        for s in shortcuts:
            if k in {_key(n) for n in s.get("names", [])} or k == _key(s.get("say", "")):
                if s.get("ask"):
                    return Action(say=s["ask"], expression="neutral", answers=s.get("answers", {}))
                if s.get("script"):
                    return Action(say=f"Opening {s.get('say', 'it')}.", script=s["script"])
                url = s.get("url", "")
                if urlsplit(url).scheme in ("http", "https"):
                    return Action(say=f"Opening {s.get('say', 'it')}.", url=url)
        if url := spoken_url(raw.group("target") if raw else target):
            return Action(say=f"Opening {urlsplit(url).netloc.removeprefix('www.')}.", url=url)
    if said_chrome and not target:
        return Action(say="Opening Chrome.", url="")
    if said_chrome or m.group(0).lower().lstrip().startswith(("go to", "take me to")):
        # clearly wanted a page, but not one I can be sure of: ask rather than guess
        return Action(say="I'm not sure which website you mean. Could you say the address, like weather dot com?",
                      expression="neutral")
    return None   # "open up about your day", "show me how"… is just conversation


WHOSE = r"(?:(?:owner'?s|loved_one'?s|my|your|our|the)\s+)?"
_YT = r"(?:you\s*tube|youtube)"
_VERB = r"(?:play|put\s+on|find\s+and\s+play|show\s+me|show|find(?:\s+me)?|search\s+for|look\s+up|open(?:\s+up)?|pull\s+up|bring\s+up|queue(?:\s+up)?)"
PLAY_RE = re.compile(LEAD + r"(?:(?:open(?:\s+up)?|go\s+to|pull\s+up|bring\s+up)\s+" + WHOSE + _YT +
                     r"\s+(?:(?:and|to|then)\s+)+" + _VERB + r"|play|put\s+on|find\s+and\s+play)\s+(?P<what>.+?)"
                     r"(?:\s+(?:on|in|from|using)\s+" + WHOSE + _YT + r")?$", re.I)
# natural requests: "would you open up a Steely Dan song and listen to it with me", "can we listen to some blues",
# "let's hear some Fogelberg", "find me a Steely Dan song", "pull up Hotel California", "show me a funny cat video"
LISTEN_RE = re.compile(LEAD + r"(?:(?:can|could|shall)\s+we\s+|let'?s\s+)?(?:open(?:\s+up)?|pull\s+up|bring\s+up|find(?:\s+me)?|"
                       r"pick(?:\s+out)?|choose|"
                       r"show\s+me|search\s+for|look\s+up|queue(?:\s+up)?|start|listen\s+to|hear|turn\s+on|watch)\s+(?P<what>.+?)$", re.I)
_TAIL = re.compile(r"(?:\s+(?:and|so|to)\s+(?:we\s+can\s+|let'?s\s+)?(?:listen|hear|play|watch)\b.*|\s+(?:with\s+me|together|for\s+(?:us|me)|"
                   r"right\s+now|on\s+youtube|from\s+youtube|please|like\s+i\s+(?:asked|said|wanted)(?:\s+\w+)*))+$", re.I)
_FILLER = re.compile(r"^(?:me\s+)?(?:some|a\s+little|a\s+bit\s+of|a\s+song\s+by|songs?\s+by|music\s+by|"
                     r"something\s+by|the\s+song|a\s+song\s+called|an?|one|random|a\s+random)\s+", re.I)
_MUSICY = re.compile(r"\b(music|songs?|album|tune|band|symphony|concerto)\b", re.I)
_VIDEO = re.compile(r"\b(videos?|clips?|cartoons?|movies?|shows?|trailers?)\b", re.I)
# "Can you do me a favor and please …", "… for me, Echo": the request is still in there
_FAVOR = re.compile(r"^.*?\b(?:do\s+me\s+a\s+favou?r|be\s+a\s+dear|i\s+was\s+wondering\s+if\s+you\s+could)\b[\s,]*(?:and\s+)?", re.I)
_END_NAME = re.compile(r"[\s,]+(?:echo|ecco|eco|eko|ekko|ecko)\W*$", re.I)


def music_artists() -> list[str]:
    """Approved artists AND genres (config/music.json): "play some Steely Dan" / "play some blues" mean music."""
    try:
        d = json.loads((ROOT / "config" / "music.json").read_text())
        return [a.lower() for a in d.get("approved_artists", []) + d.get("approved_genres", [])]
    except (OSError, ValueError):
        return []


def _requests(text: str) -> list[str]:
    """The message cut into sentences, each without "do me a favor and" in front or "…, Echo" at the end; the last
    sentence first ("YouTube is open. Please play cat videos." → "please play cat videos" is the request)."""
    out = []
    for sent in reversed([x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]):
        c = _clean(_END_NAME.sub("", _FAVOR.sub("", sent.strip())))
        if c:
            out.append(c)
        # 2026-11-03: the request can be the last part after a comma ("Sure, but instead of …, why don't you pick …")
        parts = [x for x in re.split(r",\s*", sent.strip()) if x.strip()]
        if len(parts) > 1 and (c := _clean(_END_NAME.sub("", parts[-1]))) and c not in out:
            out.append(c)
    return out


# ---- 2026-11-03 (the owner's log): "pick another Stilly Dance song" (speech recognition) and "play a different song by
# them" were searched on YouTube word for word and found "D them up" by Mister B. Now a misheard artist name is
# corrected, "them"/"another" is the artist we were just talking about, and the song we had is not picked again ----
def fix_artists(text: str, artists: list[str] | None = None) -> str:
    """"stilly dance" → "steely dan": a run of words that sounds close to a listed artist (same number of words, same
    first letters, similar spelling) is replaced by the artist's name. Only names of 8+ letters, never genres."""
    names = [a for a in (artists if artists is not None else approved_artist_names()) if len(a.replace(" ", "")) >= 8]
    words = (text or "").split()
    for a in names:
        aw = a.lower().split()
        n, i = len(aw), 0
        while i + n <= len(words):
            chunk = [re.sub(r"[^\w']", "", w).lower() for w in words[i:i + n]]
            joined = " ".join(chunk)
            if joined != a.lower() and all(c[:1] == x[:1] for c, x in zip(chunk, aw)) and \
                    difflib.SequenceMatcher(None, joined, a.lower()).ratio() >= 0.7:
                words[i:i + n] = a.lower().split()
            i += 1
    return " ".join(words)


def approved_artist_names() -> list[str]:
    try:
        return [a.lower() for a in json.loads((ROOT / "config" / "music.json").read_text()).get("approved_artists", [])]
    except (OSError, ValueError):
        return []


_OTHER = re.compile(r"\b(?:different|another|other|else|new)\b", re.I)
_REF = re.compile(r"\b(?:(?:by|from)\s+)?(?:them|him|her|those\s+guys|that\s+(?:band|group|artist|singer)|"
                  r"the\s+same\s+(?:band|group|artist|singer))\b", re.I)
_SONGWORD = re.compile(r"\b(?:songs?|ones?|tracks?|tunes?|records?|something|of\s+theirs|of\s+his|of\s+hers)\b", re.I)
_INSTEAD = re.compile(r"\binstead\s+of\s+(?:playing\s+|hearing\s+)?[\"“]?(?P<t>[^,.;?!\"”]{2,60}?)[\"”]?\s*(?:[,.;?!]|$|\s+(?:why|can|could|would|will|please|pick|play))", re.I)


def last_artist(recent: str, artists: list[str]) -> str | None:
    """The artist or genre named most recently in these words (misheard names corrected)."""
    low = fix_artists(recent or "", [a for a in artists if a in approved_artist_names()] or None).lower()
    best, at = None, -1
    for a in artists:
        i = low.rfind(a)
        if i > at:
            best, at = a, i
    return best


def _what_other(what: str, raw: str, recent: str, artists: list[str], last_play: dict | None) -> tuple[str, bool] | None:
    """For "another one" / "a different song by them" / "something by him": (artist to search, True); None if the words
    don't refer back to anything; ("", True) if they do but I can't tell who."""
    other, ref = bool(_OTHER.search(what)), bool(_REF.search(what))
    if not (other or ref):
        return None
    rest = re.sub(r"\s+", " ", _SONGWORD.sub(" ", _REF.sub(" ", _OTHER.sub(" ", what)))).strip()
    rest = re.sub(r"\b(?:instead|then|now|please)$", "", re.sub(r"^(?:an?|some|one|the)\b", "", rest).strip()).strip()
    if rest and not re.fullmatch(r"(?:by|from|of|than\s+that|than\s+this)", rest):
        # "another steely dan" → steely dan; "Another Brick in the Wall" is a song title, searched as said
        return (rest, True) if rest in artists else None
    fresh = last_play if last_play and time.time() - last_play.get("at", 0) < 3600 else None
    who = (fresh or {}).get("artist") or last_artist(recent, artists)
    if not who and not (_MUSICY.search(raw) or fresh):
        return None
    return who or "", True


# ---- 2026-11-03 (the owner's log): "can you use the same open window and choose us another Steely Dan song. Perhaps
# Reelin' in the Years?" was not a command (the LLM promised, nothing played). ----
_SAME_WIN = re.compile(r"^(?:(?:can|could|would|will)\s+you\s+|please\s+)?(?:use|keep|stay\s+in)\s+(?:the\s+)?(?:same|that|this)"
                       r"(?:\s+(?:open|music|youtube))?\s+(?:window|tab|page)\s+(?:and|to)\s+", re.I)
_SUGGEST = re.compile(r"(?:^|[.,;!?]\s*|\s)(?:perhaps|maybe|how\s+about|what\s+about|something\s+like)\s+[\"“']?"
                      r"(?P<s>[^.,;!?\"”]{2,60}?)[\"”']?\s*(?:[.,;!?]|$)", re.I)
_NOT_TITLE = re.compile(r"^(?:play|open|pick|choose|find|put|you|we|i|it|this|that|something|one|another|a|an|some|them|"
                        r"him|her|not|just|later|next|again|instead|so|too)\b", re.I)


def _suggested(text: str, artist: str) -> str | None:
    """"…another Steely Dan song. Perhaps Reelin' in the Years?" → "reelin in the years" (a song named as a suggestion)."""
    for m in _SUGGEST.finditer(text or ""):
        t = re.sub(r"\s+", " ", m.group("s").lower().replace("’", "'")).strip(" '")
        t = re.sub(r"\s+(?:by\s+(?:them|him|her|" + re.escape(artist.lower()) + r")|please|instead)$", "", t).strip()
        if t and not _NOT_TITLE.match(t) and t != artist.lower() and len(t.split()) <= 8:
            return t
    return None


def parse_play(text: str, speaker: str | None = None, artists: list[str] | None = None, recent: str = "",
               last_play: dict | None = None) -> Action | None:
    """"open YouTube and play some Steely Dan" / "play Hotel California on YouTube" / "play some Steely Dan" (a listed
    artist) / "play a random cat video" → find it on YouTube and play it in Echo's window. "play a game" stays talk."""
    artists = music_artists() if artists is None else artists
    for clean in _requests(text):
        clean = _SAME_WIN.sub("", clean)   # "use the same open window and choose …": Echo's music window is reused anyway
        m = PLAY_RE.match(clean) or LISTEN_RE.match(clean)
        if not m:
            continue
        raw = re.sub(r"^(?:us|me)\s+", "", m.group("what"))   # "choose us another …", "play me some …"
        raw = re.sub(r"\s+(?:(?:perhaps|maybe|how\s+about|what\s+about|something\s+like)\b.*|perhaps|maybe)$", "", raw).strip() or raw
        what = TRAIL_RE.sub("", _PURPOSE.sub("", _TAIL.sub("", raw))).strip()
        what = fix_artists(re.sub(r"\s+(?:songs?|tunes?|tracks?|records?)$", "", what))   # "a steely dan song" → "steely dan"
        if m.re is LISTEN_RE and re.search(_YT, what, re.I):   # "open (the loved one's) YouTube": the open-a-site rule asks whose
            continue
        rand = bool(re.search(r"\brandom\b", what, re.I))
        exclude, avoid = None, None
        if (o := _what_other(what, raw, recent, artists, last_play)):
            if not o[0]:
                return None   # "another one by them", but I don't know who: the not-done note makes me ask
            what, rand = o[0], True
            fresh = last_play if last_play and time.time() - last_play.get("at", 0) < 3600 else {}
            exclude = [v for v in [fresh.get("vid")] if v]
            avoid = [m.group("t").strip() for m in _INSTEAD.finditer(text)] or None
        for _ in range(4):   # "some music by the Eagles" → "the eagles"; "a random cat video" → "cat video"
            what = _FILLER.sub("", what).strip()
        what = re.sub(r"\s+(?:music|songs)$", "", what) if len(what.split()) > 1 else what
        if not what or len(what) > 80 or re.fullmatch(_YT, what, re.I):
            continue
        said_yt = re.search(_YT, clean, re.I) is not None
        video = bool(_VIDEO.search(raw)) and not _MUSICY.search(raw) and not any(a in what for a in artists)
        if not (said_yt or video or _MUSICY.search(raw) or any(a in what for a in artists) or exclude is not None):
            continue
        loved_one = speaker == "loved_one"
        who = LOVED_ONE_ACCOUNT if loved_one else OWNER_ACCOUNT
        from urllib.parse import quote_plus
        search = f"https://www.youtube.com/results?search_query={quote_plus(what)}" + (f"&authuser={who}" if who else "")
        if exclude is not None and (song := _suggested(text, what)):   # "… another Steely Dan song. Perhaps Reelin' …"
            name = what.title() if what in approved_artist_names() else what
            return Action(say=f"Okay, playing {song.title()} by {name}.", url=search.replace(quote_plus(what),
                          quote_plus(f"{song} {what}")), play=f"{song} {what}", exclude=exclude)
        if exclude is not None:   # "a different song by them"
            name = what.title() if what in approved_artist_names() else what
            return Action(say=f"Okay, I'll pick {'a different' if _OTHER.search(raw) else 'a'} {name} song.", url=search, play=what, random=True,
                          exclude=exclude, avoid=avoid)
        return Action(say=f"Okay, finding {'a ' if video and not what.endswith('s') else ''}{what} on YouTube.",
                      url=search, play=what, video=video, random=rand)
    return None


ACTION_ASK = re.compile(LEAD + r"(?:(?:can|could|shall)\s+we\s+|let'?s\s+)?(?:open|play|put\s+on|pull\s+up|bring\s+up|close|shut|quit|exit|"
                        r"go\s+to|launch|start|turn\s+on|queue|listen\s+to|hear|find|show\s+me|watch)\b", re.I)


_THING = re.compile(r"\b(you\s*tube|songs?|music|videos?|clips?|album|playlist|radio|channel|tunes?|tracks?|web\s*site|site|"
                    r"web\s*page|page|chrome|browser|internet|google|netflix|spotify|dot\s+com)\b|\.[a-z]{2,4}\b", re.I)


def wants_action(text: str, artists: list[str] | None = None) -> bool:
    """A request to open/play something on the computer (whether or not I recognised it): a request verb AND a sign of
    music or the web, in any sentence ("open up about your day", "listen to me" are just talk)."""
    artists = music_artists() if artists is None else artists
    for clean in _requests(text):
        if ACTION_ASK.match(clean) and (_THING.search(clean) or any(a in clean for a in artists)):
            return True
    return False


NOT_DONE = ("[Nothing was opened or played for this message: I didn't recognise it as a command. I must NOT say I'm "
            "opening, playing or putting anything on, and I don't offer to (\"Shall I open it?\"). I say I couldn't do it "
            "from those words and suggest saying, for example, \"play a cat video on YouTube\" or \"play Steely Dan on "
            "YouTube\".]")


def youtube_top(query: str, pick_random: bool = False, exclude: list[str] | None = None,
                avoid: list[str] | None = None) -> str | None:
    """Address of the top YouTube video for a search (yt-dlp, details only, ~2-3 s), or one of the top 5 at random;
    None if the lookup fails. 2026-11-03: `exclude` video ids and titles containing an `avoid` song are skipped, and so
    are hour-long compilations when picking a song at random."""
    try:
        import random

        import yt_dlp
        n = 10 if (exclude or avoid) else (5 if pick_random else 1)
        if exclude is not None and len(query.split()) <= 4:
            query = f"{query} song"   # "steely dan" alone finds full albums and interviews
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
                               "noplaylist": True, "socket_timeout": 15}) as y:
            d = y.extract_info(f"ytsearch{n}:{query}", download=False)
        entries = [e for e in (d.get("entries") or []) if e and e.get("id")]
        if exclude or avoid:
            bad = [a.lower() for a in avoid or [] if a.strip()]
            keep = [e for e in entries if e["id"] not in (exclude or [])
                    and not any(b in (e.get("title") or "").lower() for b in bad)
                    and not (e.get("duration") and e["duration"] > 900)]
            entries = keep[:5] or entries
        entries = entries or [{}]
        vid = (random.choice(entries) if pick_random else entries[0]).get("id") or ""
        return f"https://www.youtube.com/watch?v={vid}" if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None
    except Exception:  # noqa: BLE001  (no internet / YouTube changed: the search page opens instead)
        return None


def answer(text: str, answers: dict[str, str], shortcuts: list[dict] | None = None) -> Action | None:
    """The reply to one of my questions ("Your YouTube or mine?" -> "mine"), else None."""
    k = _key(text)
    for said, name in answers.items():
        if k == _key(said):
            return parse(f"open {name}", shortcuts)
    return None


# ---- 2026-11-02: closing ("close the weather channel" → Echo had no close command and only promised) ----
CLOSE_RE = re.compile(LEAD + r"(?:(?:can|could|shall)\s+we\s+|let'?s\s+)?(?:close|shut|quit|exit|get\s+rid\s+of)"
                      r"(?:\s+(?:out|down|up))?(?:\s+of)?\s+(?P<target>.+)$", re.I)
_IT = re.compile(r"^(?:that|it|this|the\s+(?:window|tab|page|site|web\s*site|browser)|that\s+(?:window|tab|page|site)|"
                 r"chrome|google\s+chrome|the\s+web\s*page)$", re.I)
_YT_HOST = re.compile(r"^(?:(?:owner'?s|loved_one'?s|my|your|the)\s+)?you\s*tube(?:\s+video)?$", re.I)


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def parse_close(text: str, last: dict | None = None, shortcuts: list[dict] | None = None) -> Action | None:
    """"close the weather channel" / "close YouTube" / "close that" (= what I opened last, if within 30 min) → an Action
    that closes Chrome tabs showing that site. "close your eyes", "we're close" → None (just talk)."""
    shortcuts = load_shortcuts() if shortcuts is None else shortcuts
    for clean in _requests(text):
        m = CLOSE_RE.match(_PURPOSE.sub("", clean))
        if not m:
            continue
        target = TRAIL_RE.sub("", m.group("target")).strip()
        if _IT.match(target):
            if last and last.get("host") and time.time() - last.get("at", 0) < 1800:
                return Action(say=f"Closing {last.get('name') or last['host']}.", close=last["host"])
            continue
        if _YT_HOST.match(target):
            return Action(say="Closing YouTube.", close="youtube.com")
        k = _key(target)
        for s in shortcuts:
            if k in {_key(n) for n in s.get("names", [])} or k == _key(s.get("say", "")):
                url = s.get("url", "")
                if urlsplit(url).scheme in ("http", "https") and _host(url):
                    return Action(say=f"Closing {s.get('say', _host(url))}.", close=_host(url))
        if (url := spoken_url(target)):
            return Action(say=f"Closing {_host(url)}.", close=_host(url))
    return None


_CLOSE_SCRIPT = """on run argv
  set h to item 1 of argv
  if application "Google Chrome" is not running then return 0
  tell application "Google Chrome"
    set n to 0
    repeat with w in windows
      set c to count of (tabs of w whose URL contains h)
      if c > 0 then
        close (tabs of w whose URL contains h)
        set n to n + c
      end if
    end repeat
    return n
  end tell
end run"""


def run_close(host: str) -> int | None:
    """Close Chrome tabs whose address contains `host`. Number closed, or None if macOS didn't let me (Automation
    permission for Google Chrome) or Chrome didn't answer. Never Echo's own window (it's on 127.0.0.1)."""
    host = (host or "").strip().lower()
    if not host or host in ("127.0.0.1", "localhost") or "." not in host:
        return None
    try:
        r = subprocess.run(["osascript", "-e", _CLOSE_SCRIPT, host], capture_output=True, text=True, timeout=15)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---- 2026-11-03: what's really playing. YouTube's Cross-Origin-Opener-Policy cuts Echo's page off from the music
# window it opens (the page sees it as "closed" within a second), and YouTube plays the next song by itself, so Echo
# asks Chrome directly which YouTube videos are open (same Automation permission as closing tabs). ----
_TABS_SCRIPT = """if application "Google Chrome" is not running then return ""
tell application "Google Chrome"
  set out to ""
  repeat with w in windows
    repeat with t in (tabs of w whose URL contains "youtube.com/watch")
      set out to out & (URL of t) & tab & (title of t) & linefeed
    end repeat
  end repeat
  return out
end tell"""


def youtube_tabs() -> list[tuple[str, str]] | None:
    """(address, page title) of every YouTube video open in Chrome, front window first. [] = none open;
    None = Chrome didn't answer or macOS didn't let me ask."""
    try:
        r = subprocess.run(["osascript", "-e", _TABS_SCRIPT], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r is None or r.returncode != 0:
        return None
    out = []
    for line in (r.stdout or "").splitlines():
        url, _, title = line.partition("\t")
        if url.strip():
            out.append((url.strip(), title.strip()))
    return out


def tab_song(title: str) -> str:
    """'(2) Steely Dan - Aja - YouTube' → 'Steely Dan - Aja'."""
    t = re.sub(r"^\(\d+\)\s*", "", title or "").strip()
    t = re.sub(r"\s*[-–]\s*YouTube$", "", t).strip()
    return t


_NOW_PLAYING = re.compile(
    r"\b(?:what(?:'s|\s+is|\s+song\s+is|\s+song's)\s+(?:this\s+)?playing\b|what(?:'s|\s+is)\s+on\s+(?:now|right\s+now)\W*$|"
    r"what\s+(?:song|tune|track|music)\s+(?:is\s+)?(?:this|that|playing|on)|"
    r"what(?:'s|\s+is)\s+(?:this|that)\s+(?:song|tune|track)|"
    r"what\s+are\s+we\s+(?:listening\s+to|hearing)|"
    r"(?:who|which\s+band)\s+(?:sings|is\s+singing|plays|is\s+playing)\s+(?:this|that|now)|"
    r"(?:what|which)\s+song\s+(?:are\s+we\s+on|came\s+on|is\s+next)|"
    r"(?:is\s+(?:this|that)|are\s+we\s+(?:still\s+)?(?:on|listening\s+to))\s+(?:still\s+)?(?:the\s+same\s+song|a\s+(?:new|different)\s+song))",
    re.I)


def asks_now_playing(text: str) -> bool:
    """"What is playing right now?", "what song is this?", "who sings this?" (not a request to play something)."""
    t = (text or "").strip()
    return bool(_NOW_PLAYING.search(t)) and not re.search(r"\b(?:play|open|put\s+on|find)\s+(?:me\s+|us\s+)?(?:a|some|another)\b", t, re.I)


# 2026-11-03 (the owner): "make the music softer and his voice louder when he talks over it". The video's own volume is
# lowered while Echo speaks and put back after (whatever the owner had set). Needs Chrome's View > Developer > "Allow
# JavaScript from Apple Events" (Chrome refuses otherwise; then the music just stays as it is).
DUCK_TO = 0.25
_DUCK_JS = ("(function(on,to){var n=0;document.querySelectorAll('video').forEach(function(v){"
            "if(on){if(!v.dataset.echoVol)v.dataset.echoVol=String(v.volume);v.volume=Math.min(v.volume,+v.dataset.echoVol*to);}"
            "else if(v.dataset.echoVol){v.volume=+v.dataset.echoVol;delete v.dataset.echoVol;}n++;});return n;})(%s,%s)")
_DUCK_SCRIPT = """on run argv
  set js to item 1 of argv
  if application "Google Chrome" is not running then return "0"
  tell application "Google Chrome"
    set n to 0
    repeat with w in windows
      repeat with t in (tabs of w whose URL contains "youtube.com/watch")
        execute t javascript js
        set n to n + 1
      end repeat
    end repeat
    return n as text
  end tell
end run"""


def duck_music(on: bool) -> str:
    """Softer (on) / back to how it was (off), for every YouTube video open in Chrome. "ok", "none" (no video open),
    "js_off" (Chrome's Apple Events JavaScript setting is off) or "failed"."""
    js = _DUCK_JS % ("true" if on else "false", DUCK_TO)
    try:
        r = subprocess.run(["osascript", "-e", _DUCK_SCRIPT, js], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    if r is None:
        return "failed"
    if r.returncode != 0:
        return "js_off" if "javascript" in (r.stderr or "").lower() else "failed"
    return "ok" if (r.stdout or "").strip() not in ("", "0") else "none"


def run(action: Action) -> bool:
    """Open it. Returns False if it couldn't be started."""
    try:
        if action.script:
            script = (ROOT / action.script).resolve()
            if ROOT / "scripts" not in script.parents or not script.is_file():
                return False
            subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        elif action.url is not None:
            url = action.url
            if action.play:
                top = youtube_top(action.play, action.random, action.exclude, action.avoid)
                if top:
                    url = top + ("&authuser=" + url.split("authuser=")[-1] if "authuser=" in url else "")
            args = ["open", "-na", "Google Chrome", "--args", "--new-window"] + ([url] if url else [])
            subprocess.run(args, check=True, timeout=15, capture_output=True)
            action.opened = url
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---- 2026-11-02: offers Echo makes himself ("would you like to go to The Weather Channel?", "I will open YouTube and
# play Do It Again by Steely Dan") → the real Action, so a "yes" does it instead of the LLM just talking about it ----
_OFFER = re.compile(r"\b(?P<kind>would\s+you\s+like\s+(?:me\s+)?to|do\s+you\s+want\s+(?:me\s+)?to|shall\s+(?:we|i)|"
                    r"should\s+(?:we|i)|how\s+about\s+(?:we\s+)?|let'?s|let\s+me|i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to)|"
                    r"we\s+(?:can|could))\s+(?:go\s+(?:and\s+)?|just\s+)?(?P<cmd>(?:open(?:\s+up)?|go\s+to|pull\s+up|bring\s+up|play|put\s+on|listen\s+to|watch|close)"
                    r"\b[^.?!]*)", re.I)
_CLAIM = re.compile(r"^(?:let\s+me|i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to))$", re.I)
YES = re.compile(r"^\W*(?:(?:oh|well|ok|okay)[,\s]+)?(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please(?:\s+do)?|go\s+ahead|do\s+it|"
                 r"let'?s\s+do\s+(?:it|that)|let'?s\s+go|sounds\s+(?:good|great)|absolutely|of\s+course|why\s+not|definitely)\b", re.I)


def offer_from_reply(reply: str, speaker: str | None = None, recent: str = "",
                     last_play: dict | None = None) -> tuple[Action, bool] | None:
    """(Action, is_claim) for the first thing in my reply I offered or announced that I can really open or play;
    is_claim = I said I'm doing it ("I will open…"), not asked ("would you like…"). None if nothing actionable."""
    for m in _OFFER.finditer((reply or "").replace("’", "'").replace("“", '"').replace("”", '"')):
        cmd = re.sub(r'["]', "", m.group("cmd")).strip()
        if cmd.lower().startswith("listen to"):
            cmd = "play " + cmd[len("listen to"):].strip()
        if cmd.lower().startswith("watch"):
            cmd = "play " + cmd[len("watch"):].strip()
        act = parse_close(cmd) or parse_play(cmd, speaker, recent=recent, last_play=last_play) or parse(cmd)
        if act and act.play and not act.video and not act.exclude:   # "…some Steely Dan. Shall I play Do It Again?"
            arts = music_artists()
            if not any(a in act.play for a in arts) and (who := last_artist(reply, [a for a in arts if a in
                                                                                    approved_artist_names()])):
                act.play = f"{act.play} {who}"
        if act and (act.url is not None or act.script or act.play or act.close):
            return act, bool(_CLAIM.match(m.group("kind").strip()))
    return None


def is_yes(text: str) -> bool:
    """A short yes ("Yes, let's do it.", "Sure.", "Okay go ahead") that doesn't ask for something else."""
    t = (text or "").strip()
    return bool(YES.match(t)) and len(t.split()) <= 12 and not parse(t) and not parse_play(t)


# ---- 2026-11-03 (the owner's log): Echo opened The Weather Channel, then "what do you see on the weather channel?" got "I
# can't see the screen". A question about the page I just opened now reads that page (with the full browser, as Chrome
# shows it) and the answer comes from it. "What's the weather like?" reads the weather shortcut even if it isn't open. ----
_PAGE_Q = re.compile(r"\b(?:what\s+(?:do|can|did)\s+you\s+see|what\s+(?:does|did)\s+(?:it|that|the\s+(?:page|site|screen|forecast|"
                     r"website))\s+say|what(?:'s|\s+is|\s+does\s+it\s+(?:say|show))\s+on\s+(?:it|there|that|the\s+(?:page|screen|"
                     r"site|website|channel|weather\s+channel))|read\s+(?:it|that|this|me|the\s+(?:page|forecast|site))|"
                     r"tell\s+me\s+what\s+(?:it|the\s+page|you\s+see)|look\s+at\s+(?:it|the\s+page|the\s+screen)|"
                     r"can\s+you\s+(?:see|read)\s+(?:it|that|the)|what\s+does\s+it\s+(?:say|show))\b", re.I)
_WEATHER_Q = re.compile(r"\b(?:what(?:'s|\s+is|\s+will)\s+the\s+(?:weather|forecast|temperature)|how(?:'s|\s+is)\s+the\s+weather|"
                        r"what(?:'s|\s+is)\s+it\s+like\s+outside|is\s+it\s+(?:going\s+to\s+)?(?:rain|snow)(?:ing)?|"
                        r"(?:check|see|look\s+at)\s+(?:the\s+)?(?:weather|forecast)|how\s+(?:hot|cold|warm)\s+is\s+it)\b", re.I)
PAGE_S = 1800


def weather_url(shortcuts: list[dict] | None = None) -> str | None:
    for s in load_shortcuts() if shortcuts is None else shortcuts:
        if any("weather" in n.lower() for n in s.get("names", [])) and urlsplit(s.get("url", "")).scheme == "https":
            return s["url"]
    return None


def page_to_read(text: str, last: dict | None, shortcuts: list[dict] | None = None) -> tuple[str, str] | None:
    """(address, name) of the page a question is about: the page I opened in the last 30 min, if asked what's on it;
    the weather page for a weather question. None otherwise (also for commands, which are handled before)."""
    t = (text or "").replace("’", "'")
    if last and (last.get("url") or last.get("host")) and time.time() - last.get("at", 0) < PAGE_S:
        url = last.get("url") or f"https://{last['host']}/"
        if urlsplit(url).scheme in ("http", "https") and (_PAGE_Q.search(t) or (
                "weather" in _host(url) and _WEATHER_Q.search(t))):
            return url, last.get("name") or _host(url)
    if _WEATHER_Q.search(t) and (url := weather_url(shortcuts)):
        return url, "The Weather Channel"
    return None


def page_note(name: str) -> str:
    return (f"[{name} is open in Chrome on the computer, and I just read it (the web page below is what it shows). I answer "
            "from it in my own words: the main things, short, like telling someone what I see. I never say I can't see "
            "the screen.]")
