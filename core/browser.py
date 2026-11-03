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


# 2026-11-02 (the owner's log): "I think that we could uh open the weather channel and see what the weather's going to be
# like" wasn't recognised. Hedges in front and a purpose clause behind are now taken off before matching.
_UHM = re.compile(r"\b(?:uh+|um+|er+|erm|hmm+)\b", re.I)
_INTERJ = re.compile(r"^(?:(?:well|so|okay|ok|oh|yeah|yes|yep|sure|alright|all\s+right|hey|hi|good|great|cool|nice|thanks|"
                     r"thank\s+you|now|and|then)\s+)+", re.I)
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
    return out


def parse_play(text: str, speaker: str | None = None, artists: list[str] | None = None) -> Action | None:
    """"open YouTube and play some Steely Dan" / "play Hotel California on YouTube" / "play some Steely Dan" (a listed
    artist) / "play a random cat video" → find it on YouTube and play it in Echo's window. "play a game" stays talk."""
    artists = music_artists() if artists is None else artists
    for clean in _requests(text):
        m = PLAY_RE.match(clean) or LISTEN_RE.match(clean)
        if not m:
            continue
        raw = m.group("what")
        what = TRAIL_RE.sub("", _PURPOSE.sub("", _TAIL.sub("", raw))).strip()
        what = re.sub(r"\s+(?:songs?|tunes?|tracks?|records?)$", "", what)       # "a steely dan song" → "steely dan"
        if m.re is LISTEN_RE and re.search(_YT, what, re.I):   # "open (the loved one's) YouTube": the open-a-site rule asks whose
            continue
        rand = bool(re.search(r"\brandom\b", what, re.I))
        for _ in range(4):   # "some music by the Eagles" → "the eagles"; "a random cat video" → "cat video"
            what = _FILLER.sub("", what).strip()
        what = re.sub(r"\s+(?:music|songs)$", "", what) if len(what.split()) > 1 else what
        if not what or len(what) > 80 or re.fullmatch(_YT, what, re.I):
            continue
        said_yt = re.search(_YT, clean, re.I) is not None
        video = bool(_VIDEO.search(raw)) and not _MUSICY.search(raw) and not any(a in what for a in artists)
        if not (said_yt or video or _MUSICY.search(raw) or any(a in what for a in artists)):
            continue
        loved_one = speaker == "loved_one"
        who = LOVED_ONE_ACCOUNT if loved_one else OWNER_ACCOUNT
        from urllib.parse import quote_plus
        search = f"https://www.youtube.com/results?search_query={quote_plus(what)}" + (f"&authuser={who}" if who else "")
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


def youtube_top(query: str, pick_random: bool = False) -> str | None:
    """Address of the top YouTube video for a search (yt-dlp, details only, ~2-3 s), or one of the top 5 at random;
    None if the lookup fails."""
    try:
        import random

        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
                               "noplaylist": True, "socket_timeout": 15}) as y:
            d = y.extract_info(f"ytsearch{5 if pick_random else 1}:{query}", download=False)
        entries = [e for e in (d.get("entries") or []) if e and e.get("id")] or [{}]
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
                top = youtube_top(action.play, action.random)
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
                    r"we\s+(?:can|could))\s+(?P<cmd>(?:open(?:\s+up)?|go\s+to|pull\s+up|bring\s+up|play|put\s+on|listen\s+to|watch|close)"
                    r"\b[^.?!]*)", re.I)
_CLAIM = re.compile(r"^(?:let\s+me|i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to))$", re.I)
YES = re.compile(r"^\W*(?:(?:oh|well|ok|okay)[,\s]+)?(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please(?:\s+do)?|go\s+ahead|do\s+it|"
                 r"let'?s\s+do\s+(?:it|that)|let'?s\s+go|sounds\s+(?:good|great)|absolutely|of\s+course|why\s+not|definitely)\b", re.I)


def offer_from_reply(reply: str, speaker: str | None = None) -> tuple[Action, bool] | None:
    """(Action, is_claim) for the first thing in my reply I offered or announced that I can really open or play;
    is_claim = I said I'm doing it ("I will open…"), not asked ("would you like…"). None if nothing actionable."""
    for m in _OFFER.finditer((reply or "").replace("’", "'").replace("“", '"').replace("”", '"')):
        cmd = re.sub(r'["]', "", m.group("cmd")).strip()
        if cmd.lower().startswith("listen to"):
            cmd = "play " + cmd[len("listen to"):].strip()
        if cmd.lower().startswith("watch"):
            cmd = "play " + cmd[len("watch"):].strip()
        act = parse_close(cmd) or parse_play(cmd, speaker) or parse(cmd)
        if act and (act.url is not None or act.script or act.play or act.close):
            return act, bool(_CLAIM.match(m.group("kind").strip()))
    return None


def is_yes(text: str) -> bool:
    """A short yes ("Yes, let's do it.", "Sure.", "Okay go ahead") that doesn't ask for something else."""
    t = (text or "").strip()
    return bool(YES.match(t)) and len(t.split()) <= 12 and not parse(t) and not parse_play(t)
