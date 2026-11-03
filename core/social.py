"""Social cognition (CA 2.0 Phase 5): small, transparent rules for three failures measured on 2026-11-03
(scripts/eval_social.py, real model, copy of memory):

1. Names — an unrecognised voice was greeted with the loved one's name (2/2 runs) and the owner was once called by the
   loved one's name. The prompt already says not to; the model does it anyway. blocked_names() tells the reply filter
   (core/schema.py NameFilter) which names may NOT be used to address the person speaking: the reply is fixed before it
   is shown or spoken. The names come from this install's settings (core/people.py).
2. "I'm fine, really" right after "My brother passed away last month" got "That is good to hear" (2/2). If someone
   shared something painful recently and now brushes it off, a one-time note asks for a gentle acknowledgement.
3. "It's freezing in here" got "would you like me to turn on a heater / adjust the settings" (2/2): Echo can't. When a
   message is about something physical in the room, a one-time note says what he can't do.
Notes start with "[Social note:" and are stripped from the stored conversation (never repeated later). Interpretations
are offered as possibilities, never stated as facts about the person's feelings.
"""
from __future__ import annotations

import re
import time

from core import people

SAD = re.compile(r"\b(?:passed away|died|funeral|lost (?:my|our|his|her)|cancer|diagnos\w*|hospital|divorc\w*|"
                 r"miscarriage|laid off|lost my job|put (?:him|her) down|put to sleep|grief|grieving|"
                 r"(?:really |so |very )?(?:sad|depressed|lonely|heartbroken))\b", re.I)
BRUSH_OFF = re.compile(r"^\W*(?:(?:i'?m|i am) (?:fine|ok|okay|alright|all right|good)|it'?s (?:fine|ok|okay|nothing)|"
                       r"don'?t worry(?: about (?:it|me))?|no big deal|i'?ll (?:be|get) (?:fine|ok|over it)|"
                       r"(?:never ?mind|forget it))\b", re.I)
PHYSICAL = re.compile(r"\b(?:freezing|(?:so|too|really) (?:cold|hot|dark|bright|loud|noisy)|boiling in here|"
                      r"turn (?:up|down|on|off) the (?:heat|air|lights?|tv|fan)|can'?t see|it'?s dark)\b", re.I)
RECENT_S = 24 * 3600


def blocked_names(speaker: str | None, cfg=None) -> list[str]:
    """Names that must not be used to address whoever is speaking (from this install's settings, core/people.py)."""
    if speaker == "unknown":
        return people.names()             # not sure who it is: no names at all
    if speaker == "owner":
        return [n for n in [people.loved_one()] if n]
    if speaker == "loved_one":
        return [n for n in [people.owner()] if n]
    return []                             # typed: as before


def recent_pain(turns: list[dict], now: float | None = None) -> str | None:
    """The person's own recent words about something painful (last 8 messages, last 24 h), if any."""
    now = now or time.time()
    for t in reversed(turns[-8:]):
        if t.get("role") == "user" and now - (t.get("ts") or now) < RECENT_S and SAD.search(t.get("content") or ""):
            return re.sub(r"\s+", " ", t["content"]).strip()[:160]
    return None


def note(text: str, turns: list[dict], now: float | None = None) -> str:
    """The one-time social note for this message ("" when none applies)."""
    out = []
    if len(text.split()) <= 12 and BRUSH_OFF.search(text) and (pain := recent_pain(turns, now)):
        out.append(f"[Social note: earlier they told me: \"{pain}\". \"{text.strip()[:60]}\" may not be the whole story. "
                   "I gently acknowledge what they shared and leave room to talk; I don't say \"good to hear\" or change "
                   "the subject, and I don't tell them how they feel.]")
    if PHYSICAL.search(text):
        out.append("[Social note: this is about something physical in the room. I can't change heat, lights or devices "
                   "and I never offer to; I respond to how they feel and may suggest what they could do.]")
    return "\n".join(out)
