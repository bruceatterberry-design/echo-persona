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
    re.compile(r"\b" + _VERB + r"\s+(?:it|that|this|that\s+one|this\s+one|the\s+one)(?:\s+to\s+(?:me|us))?(?:\s+(?:again|please|now|then))*"
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


# the owner showing ME something ("I'll show you a before and after picture"): not a request for one of my photos
_BRUCE_SHOWS = re.compile(r"\b(?:i'?ll|i will|i'?m (?:going to|gonna)|let me|i want to|i can|i'?d like to|i'?ll be able to)"
                          r"\s+(?:\w+\s+){0,2}show\s+you\b", re.I)


def asks_to_show(text: str) -> bool:
    t = (text or "").replace("’", "'")
    return not asks_to_close(t) and not _BRUCE_SHOWS.search(t) and any(r.search(t) for r in _SHOW)


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


def pick(text: str, store, last: str | None, tie_ok: bool = False) -> str | None:
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
    if last in best:
        return last                         # a tie: the one we were talking about
    if tie_ok and best:
        return sorted(best)[0]              # my own offer ("the picture of the owner and Daisy"): any of those fits it
    return None                             # no match: I ask which


# ---- the owner, 2026-11-05: "When he mentions pictures he has no context for I'd like him to open the picture for me to see,
# with a button to close it, or close it automatically when our conversation moves on to an unrelated topic." ----
# Before: Echo asked "I have a photo from October 2026 of a man in a green jacket… is that you?" with nothing on screen,
# the owner couldn't tell what was happening that day, and Echo filled the gap with an earlier photo (Daisy on the couch).
PHOTO_TALK = re.compile(r"\b(?:" + _P + r")\b", re.I)
_LOOSE = set("""was were what that's thats day know don't dont really think about been there here when where who how why
did does doing done good great nice yeah yep nope not no all also very much more some time today well right going
something anything thing things look looks looking shows showing taken wearing""".split())


def description(store, sha: str | None) -> str:
    if not sha:
        return ""
    r = store.db.execute("SELECT content FROM memories WHERE source = ? AND hidden IS NULL", (f"photo:{sha}",)).fetchone()
    return r[0] if r else ""


def _shared(text: str, desc: str) -> int:
    return len((_words(text) - _LOOSE) & (_words(desc) - _LOOSE))


def reply_shows(reply: str, desc: str, asked: bool) -> bool:
    """My reply is about this photo, so it goes on screen: I asked about it, or I talk about a photo and use ≥ 2 of its
    words ("…from October 2026 of a man in a green jacket")."""
    if not desc:
        return False
    return asked or (bool(PHOTO_TALK.search(reply or "")) and _shared(reply, desc) >= 2)


def moved_on(text: str, desc: str, prev_reply: str, answering_photo: bool) -> bool:
    """Close the photo on screen? Not while they answer my photo question, mention a photo, or use its words ("the blue
    shirt"); not for a short answer ("Yes, that's me") right after I talked about it. Anything else is a new topic."""
    if answering_photo or PHOTO_TALK.search(text or "") or (desc and _shared(text, desc) >= 1):
        return False
    prev_about = bool(PHOTO_TALK.search(prev_reply or "")) or (bool(desc) and _shared(prev_reply, desc) >= 2)
    return not (prev_about and len(re.findall(r"[A-Za-z']+", text or "")) <= 6)
