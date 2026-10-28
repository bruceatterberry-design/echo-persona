"""Wake-phrase detection on transcripts ("Hey Echo").

Speech recognition spells the name several ways ("eko", "ecco", "ekko"), so matching is forgiving about the name
but requires a greeting word in front of it, to avoid waking on every "echo" in conversation ("there's an echo in here").
"""
from __future__ import annotations

import re

GREETING = r"(?:hey|hay|hei|hi|okay|ok)"
WEAK_GREETING = r"(?:a|ay|uh|eh)"  # Whisper's mishearings of "hey": only trusted at the start
NAME = r"(?:echo|echoes|eko|ecco|ekko|ecko|eco|eggo)"
WAKE_RE = re.compile(rf"(?:\b{GREETING}|^{WEAK_GREETING})[\s,]+{NAME}\b")


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def detect_wake(text: str) -> tuple[bool, str]:
    """Return (woke, command). The command is whatever came after the wake phrase."""
    norm = normalize(text)
    m = WAKE_RE.search(norm)
    if not m:
        return False, ""
    # recover the command from the original text, so punctuation/capitals survive for display
    after = norm[m.end():].strip()
    if not after:
        return True, ""
    words = after.split()
    orig = re.split(r"\s+", text.strip())
    command = " ".join(orig[-len(words):]) if len(words) <= len(orig) else after
    return True, command.lstrip(" ,.!?").strip()


END_PHRASE = (r"(?:that'?s all|that is all|that'?ll be all|good ?bye|bye(?: bye)?|stop listening|go to sleep|"
              r"we'?re done|i'?m done|end (?:the )?conversation|never ?mind)")
END_TAIL = r"(?:\s+(?:for now|now|for today|then|thanks|thank you|echo|okay|ok))*"
# The ending phrase must come at the end of what was said, so "my goodbye party" doesn't end a conversation.
END_RE = re.compile(rf"\b{END_PHRASE}{END_TAIL}$")

# Things Whisper invents from noise or silence. Never treat these as speech in a conversation.
HALLUCINATIONS = {"you", "thanks for watching", "thank you for watching", "please subscribe",
                  "subscribe", "bye", "the end", "so", "uh", "um", "hmm"}


def detect_end(text: str) -> bool:
    """The owner is ending the conversation ("that's all", "goodbye", "stop listening"...)."""
    return bool(END_RE.search(normalize(text)))


def is_noise_transcript(text: str) -> bool:
    n = normalize(text)
    return not n or n in HALLUCINATIONS
