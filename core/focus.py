"""Shared cognitive workspace, conversation part (CA 2.0 Phase 3). Two measured gaps, two small, transparent fixes.

1. Carry-over after a break (Test A, 11-02 baseline: "what did I say I have to do first?" — never answered "fix the
   trellis"). After 30 min of quiet the conversation window keeps only the last 8 turns, and notes are written only
   once 16 turns are out of view — so up to 15 turns simply vanished. carryover() returns the people's own words from
   those turns (verbatim: no model, nothing invented), newest last, until a conversation note covers them. It changes
   only when the window resets or a note is written, so it sits in the stable part of the prompt (no extra re-reading).

2. Following the topic (Test B: "what season does it look like it was taken in?" right after the dam photo was
   answered about a different photo, 2/2 runs — the memory search matched the word "season"). A message that refers
   back ("it", "that one", "those two") is searched together with the previous message, so the search stays on topic.

Everything here is computed from the conversation log; nothing is written to memory.
"""
from __future__ import annotations

import re
import time

from core import people

REF = re.compile(r"\b(?:it|its|it's|that|this|those|these|them|they|there|the other|which one|which of|"
                 r"the (?:first|second|third|last|same|next) (?:one|photo|picture|thing))\b", re.I)
# words that make a message stand on its own (it names its subject), even with "that" or "it" in it
CARRY_TURNS = 6          # at most this many of the people's own messages
CARRY_CHARS = 220        # each, shortened
CARRY_MAX_AGE = 24 * 3600


def is_referential(text: str) -> bool:
    """Short follow-ups that point back at the topic ("which of those two was at night?")."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    return bool(REF.search(text or "")) and len(words) <= 18


def expand_query(text: str, previous_user: str | None) -> str:
    """The memory-search query for a referential follow-up: the previous message gives the topic."""
    if previous_user and is_referential(text):
        return previous_user.strip()[-300:] + "\n" + text
    return text


def carryover(store, now: float | None = None) -> tuple[str, list[int]]:
    """People's own words from turns that left the window after a break and no note covers yet. ("", []) if none."""
    now = now or time.time()
    lo = max(int(store.get_meta("summarized_upto", "0")), store._view_from())
    hi = int(store.get_meta("window_from", "0"))
    if hi <= lo + 1:
        return "", []
    rows = store.db.execute("SELECT id, role, content, ts, speaker FROM turns WHERE id > ? AND id < ? AND role='user' "
                            "AND ts > ? ORDER BY id DESC LIMIT ?", (lo, hi, now - CARRY_MAX_AGE, CARRY_TURNS)).fetchall()
    if not rows:
        return "", []
    lines = []
    for r in reversed(rows):
        who = people.display(r["speaker"]) if r["speaker"] not in (None, "", "unknown", "typed") else "They"
        said = re.sub(r"\s+", " ", r["content"]).strip()
        lines.append(f"- {who} said: \"{said[:CARRY_CHARS]}{'…' if len(said) > CARRY_CHARS else ''}\"")
    return ("Earlier in this conversation (before the latest break; their exact words, oldest first):\n"
            + "\n".join(lines)), [r["id"] for r in rows]


def previous_user(store) -> str | None:
    r = store.db.execute("SELECT content FROM turns WHERE role='user' AND id > ? ORDER BY id DESC LIMIT 1",
                         (store._view_from(),)).fetchone()
    return r["content"] if r else None
