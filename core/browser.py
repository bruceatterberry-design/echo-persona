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

# "can you please open up chrome and go to", "take me to", "pull up a chrome window with"…
LEAD = r"^(?:(?:(?:hey|hi|okay|ok|so|um|uh)\s+)*(?:echo|ekko|eko|ecco|eco)[,.]?\s+)?(?:(?:can|could|would|will)\s+you\s+|please\s+|i\s+(?:want|need)\s+(?:you\s+)?to\s+)*"
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
            args = ["open", "-na", "Google Chrome", "--args", "--new-window"] + ([action.url] if action.url else [])
            subprocess.run(args, check=True, timeout=15, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
