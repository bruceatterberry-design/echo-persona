"""Open web pages when asked: "Hey Echo, open the Weather Channel" / "go to weather dot com".

Recognized before the LLM sees the message, so an address is only ever one that was said out loud or saved as a
shortcut in config/browser.json; the model never makes one up. Only http/https pages open (in your normal Chrome),
or one of Echo's own scripts named in that file. Anything that isn't clearly a request to open something goes to the
conversation as usual.
"""
from __future__ import annotations

import json
import re
import subprocess
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
TRAIL_RE = re.compile(r"(?:\s+(?:please|for\s+me|now|in\s+(?:google\s+)?chrome|on\s+the\s+computer|thanks?(?:\s+you)?))+$", re.I)
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
    expression: str = "smiling"
    answers: dict[str, str] | None = None   # after a question: short replies ("mine") -> shortcut to open


def _clean(s: str) -> str:
    s = s.strip().lower().replace("’", "'")
    s = re.sub(r"[\"“”!?,]+", "", s).rstrip(".").strip()
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
    """An Action if the message asks to open something, else None (a normal conversation turn)."""
    m = REQUEST_RE.match(_clean(text))
    if not m:
        return None
    target, said_chrome = m.group("target").strip(), bool(m.group("chrome"))
    raw = REQUEST_RE.match(TRAIL_RE.sub("", text.strip().replace("’", "'").rstrip(".!?\"” ")))   # typed addresses as given
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
PLAY_RE = re.compile(LEAD + r"(?:(?:open(?:\s+up)?|go\s+to|pull\s+up)\s+" + WHOSE + _YT + r"\s+(?:and\s+|to\s+)?)?"
                     r"(?:play|put\s+on|find\s+and\s+play)\s+(?P<what>.+?)"
                     r"(?:\s+(?:on|in|from|using)\s+" + WHOSE + _YT + r")?$", re.I)
# natural requests: "would you open up a Steely Dan song and listen to it with me", "can we listen to some blues",
# "let's hear some Fogelberg", "find me a Steely Dan song", "pull up Hotel California"
LISTEN_RE = re.compile(LEAD + r"(?:(?:can|could|shall)\s+we\s+|let'?s\s+)?(?:open(?:\s+up)?|pull\s+up|bring\s+up|find(?:\s+me)?|"
                       r"queue(?:\s+up)?|start|listen\s+to|hear|turn\s+on)\s+(?P<what>.+?)$", re.I)
_TAIL = re.compile(r"(?:\s+(?:and|so|to)\s+(?:we\s+can\s+|let'?s\s+)?(?:listen|hear|play)\b.*|\s+(?:with\s+me|together|for\s+(?:us|me)|"
                   r"right\s+now|on\s+youtube|from\s+youtube))+$", re.I)
_FILLER = re.compile(r"^(?:me\s+)?(?:some|a\s+little|a\s+bit\s+of|a\s+song\s+by|songs?\s+by|music\s+by|"
                     r"something\s+by|the\s+song|a\s+song\s+called|an?|one)\s+", re.I)
_MUSICY = re.compile(r"\b(music|songs?|album|tune|band|symphony|concerto)\b", re.I)


def music_artists() -> list[str]:
    """Approved artists AND genres (config/music.json): "play some Steely Dan" / "play some blues" mean music."""
    try:
        d = json.loads((ROOT / "config" / "music.json").read_text())
        return [a.lower() for a in d.get("approved_artists", []) + d.get("approved_genres", [])]
    except (OSError, ValueError):
        return []


def parse_play(text: str, speaker: str | None = None, artists: list[str] | None = None) -> Action | None:
    """"open YouTube and play some Steely Dan" / "play Hotel California on YouTube" / "play some Steely Dan" (a listed
    artist) → find it on YouTube and open it. Plain "play a game" etc. stays conversation."""
    clean = _clean(text)
    m = PLAY_RE.match(clean) or LISTEN_RE.match(clean)
    if not m:
        return None
    what = _TAIL.sub("", m.group("what")).strip()
    what = re.sub(r"\s+(?:songs?|tunes?|tracks?|records?)$", "", what)       # "a steely dan song" → "steely dan"
    if m.re is LISTEN_RE and re.search(_YT, what, re.I):   # "open (the loved one's) YouTube": the open-a-site rule asks whose
        return None
    for _ in range(3):   # "some music by the Eagles" → "the eagles"
        what = _FILLER.sub("", what).strip()
    what = re.sub(r"\s+(?:music|songs)$", "", what) if len(what.split()) > 1 else what
    if not what or len(what) > 80:
        return None
    artists = music_artists() if artists is None else artists
    said_yt = re.search(_YT, clean, re.I) is not None
    if not (said_yt or _MUSICY.search(m.group("what")) or any(a in what for a in artists)):
        return None
    loved_one = speaker == "loved_one"
    who = LOVED_ONE_ACCOUNT if loved_one else OWNER_ACCOUNT
    from urllib.parse import quote_plus
    search = f"https://www.youtube.com/results?search_query={quote_plus(what)}" + (f"&authuser={who}" if who else "")
    return Action(say=f"Okay, finding {what} on YouTube.", url=search, play=what)


ACTION_ASK = re.compile(LEAD + r"(?:(?:can|could|shall)\s+we\s+|let'?s\s+)?(?:open|play|put\s+on|pull\s+up|bring\s+up|"
                        r"go\s+to|launch|start|turn\s+on|queue|listen\s+to|hear|find|show\s+me)\b", re.I)


_THING = re.compile(r"\b(you\s*tube|songs?|music|video|album|playlist|radio|channel|tunes?|tracks?|web\s*site|site|"
                    r"web\s*page|page|chrome|browser|internet|google|netflix|spotify|dot\s+com)\b|\.[a-z]{2,4}\b", re.I)


def wants_action(text: str, artists: list[str] | None = None) -> bool:
    """A request to open/play something on the computer (whether or not I recognised it): a request verb AND a sign of
    music or the web ("open up about your day", "listen to me" are just talk)."""
    clean = _clean(text)
    if not ACTION_ASK.match(clean):
        return False
    artists = music_artists() if artists is None else artists
    return bool(_THING.search(clean)) or any(a in clean for a in artists)


NOT_DONE = ("[Nothing was opened or played for this message: I didn't recognise it as a command. I must NOT say I'm "
            "opening, playing or putting anything on. I say I couldn't do it from those words and suggest saying, for "
            "example, \"play Steely Dan on YouTube\" or \"open the owner's YouTube\".]")


def youtube_top(query: str) -> str | None:
    """Address of the top YouTube video for a search (yt-dlp, details only, ~2-3 s), or None."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
                               "noplaylist": True, "socket_timeout": 15}) as y:
            d = y.extract_info(f"ytsearch1:{query}", download=False)
        vid = (d.get("entries") or [{}])[0].get("id") or ""
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
                top = youtube_top(action.play)
                if top:
                    url = top + ("&authuser=" + url.split("authuser=")[-1] if "authuser=" in url else "")
            args = ["open", "-na", "Google Chrome", "--args", "--new-window"] + ([url] if url else [])
            subprocess.run(args, check=True, timeout=15, capture_output=True)
            action.opened = url
        return True
    except (OSError, subprocess.SubprocessError):
        return False
