"""Showing a photo I remember on the screen (the owner, 2026-11-03).

Before this, "Can you show me the picture?" made me offer to "open your photo gallery", which nothing could do, so every
"Yes" got the same offer again. Now the request (or a yes to my offer to show it) is handled before the LLM: I show the
photo we're talking about, from the approved photo memories only (source photo:<sha16>, not hidden), as the upright
1024 px copy the Photos page already serves. Removed or un-approved photos are hidden memories, so they're never shown.
"""
from __future__ import annotations

import re

PHOTO_WORD = r"(?:pictures?|photos?|photographs?|pics?|snapshots?|gallery)"
_P = PHOTO_WORD[3:-1]
_VERB = r"(?:show\s+(?:me|us|her|him)|(?:can|could|may)\s+(?:i|we)\s+(?:see|look\s+at|have\s+a\s+look\s+at)|" \
        r"let\s+(?:me|us)\s+(?:see|look\s+at)|(?:want|like|love)\s+to\s+(?:see|look\s+at)|show|open|pull\s+up|" \
        r"bring\s+up|put\s+up|display)"
_SHOW = [
    # a photo word anywhere after the verb: "show me the picture of Anna", "open your photo gallery"
    re.compile(r"\b" + _VERB + r"\b[^.?!]*\b(?:" + _P + r")\b", re.I),
    # only "it/that/this one" as the whole object: "Can I see it?", "Show me that one, please." (not "let me see, that was…")
    re.compile(r"\b" + _VERB + r"\s+(?:it|that|this|that\s+one|this\s+one|the\s+one)(?:\s+(?:again|please|now|then))*"
               r"\s*(?:,?\s*please)?\s*[.?!]*\s*$", re.I),
]
_CLOSE = re.compile(r"\b(?:(?:close|hide|put\s+away|take\s+away|take\s+down|stop\s+showing)\b[^.?!]*\b(?:" + _P
                    + r"|it|that)\b|(?:put|take)\s+(?:it|that|the\s+(?:" + _P + r"))\s+(?:away|down))", re.I)
_OFFERED = re.compile(r"\b(?:would\s+you\s+like|do\s+you\s+want|want\s+me\s+to|shall\s+i|should\s+i|"
                      r"how\s+about|like\s+to\s+see)\b[^.?!]*\b(?:" + PHOTO_WORD[3:-1] + r"|see\s+it|look\s+at\s+it)\b",
                      re.I)
_STOP = set("""a an the me us you i we it that this one of to see show look at can could may please let want like love
would will open pull bring put up display picture pictures photo photos photograph photographs pic pics snapshot
snapshots gallery my your our his her with and in on from again now so just some yes ok okay echo hey owner honey echo
there those these back sure yeah real quick big screen kind little have get give take another other""".split())


def asks_to_show(text: str) -> bool:
    t = (text or "").replace("’", "'")
    return not asks_to_close(t) and any(r.search(t) for r in _SHOW)


def asks_to_close(text: str, showing: bool = False) -> bool:
    """ "Close the picture" (always), or "close it / put that away" while a photo is on screen."""
    t = (text or "").replace("’", "'")
    m = _CLOSE.search(t)
    return bool(m) and (showing or bool(re.search(r"\b(?:" + _P + r")\b", m.group(0), re.I)))


def offered(reply: str) -> bool:
    """My last reply offered to show/open a photo ("Would you like me to open your photo gallery?")."""
    return bool(_OFFERED.search((reply or "").replace("’", "'")))


def sha_of(source: str | None) -> str | None:
    s = source or ""
    return s[6:] if s.startswith("photo:") and re.fullmatch(r"[0-9a-f]{16}", s[6:]) else None


def top_photo(hits) -> str | None:
    """The best-ranked photo memory among this turn's memory hits (they come best first)."""
    for h in hits or []:
        if (sha := sha_of(getattr(h.memory, "source", None))):
            return sha
    return None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower().replace("'s", "")) if w not in _STOP and len(w) > 2}


def pick(text: str, store, last: str | None) -> str | None:
    """Which photo to show: one the words name ("show me the picture of Anna and Emma"), else the one we were just
    talking about (last), else None (then I ask which one)."""
    rows = store.db.execute("SELECT content, source FROM memories WHERE source LIKE 'photo:%' AND hidden IS NULL").fetchall()
    live = {sha_of(r[1]): r[0] for r in rows if sha_of(r[1])}
    want = _words(text)
    if not want:
        return last if last in live else None
    scored = sorted(((len(want & _words(c)), s) for s, c in live.items()), reverse=True)
    best = [s for n, s in scored if n == scored[0][0] and n > 0] if scored else []
    if len(best) == 1:
        return best[0]
    return last if last in best else None   # a tie: the one we were talking about; no match: I ask which
