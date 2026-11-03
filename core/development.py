"""Developmental learning (CA 2.0 Phase 8): does each lesson the owner taught Echo actually work?

Lessons (core/trainer.py → data/lessons.json) are added to Echo's instructions and were never checked again. The
directive (§10): "evaluate the strategy on future interactions; retain improvements only when supported by evidence."
This module measures, from Echo's real conversation (the local conversation log, nothing sent anywhere), how often a
lesson's target behaviour happens before and after the lesson was added. Plain rules, no model call. It REPORTS only:
a lesson that isn't working is flagged for the owner; nothing is switched off or rewritten automatically.

Measured 2026-11-03 (real turns, my test turns left out): "don't open with a stock phrase + the owner" was added 11-02 07:19
and openings like "That's wonderful to hear, the owner;" still came in 12 of 72 replies after it — the rule alone wasn't
enough. Each check below names the lesson it measures by a phrase in the lesson's own text, so it follows the lesson
even if ids change; lessons with no automatic check (e.g. the poem rule) are listed as "not measured automatically".
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Callable

from core import people

PRAISE = r"(?:wonderful|great|lovely|nice|good|exciting|amazing|special|kind|fantastic|beautiful|clever|fun)"
def stock_opening() -> re.Pattern:
    """"That's wonderful, <name>;" / "That sounds great." / "It's so nice to hear." (names from core/people.py)."""
    n = people.alternation()
    return re.compile(r"^\W*(?:\[[a-z]+\]\s*)?(?:(?:that(?:'s|’s| is| was| sounds| must be)|what an?|oh|ah|wow)\b"
                      r"[^.;!?\n]{0,30}\b(?:" + PRAISE[3:-1] + "|" + n + r")\b|"
                      r"(?:it(?:'s|’s| is| sounds)) (?:so |really |very )?" + PRAISE + r"(?: (?:news|to hear))?[,.;!]|"
                      r"i'?m (?:so )?glad[^.;!?\n]{0,20}\b(?:" + n + r")\b)", re.I)
SHORT_MSG = re.compile(r"^\W*(?:ok(?:ay)?|thanks?|thank you|yes|yeah|yep|no|nope|goodnight|good night|cool|great|"
                       r"alright|all right|sure|got it|sounds good)\W*$", re.I)
OLD_TOPIC = re.compile(r"\b(?:weather|forecast|cat videos?)\b", re.I)
NARRATE = re.compile(r"^\W*(?:\[[a-z]+\]\s*)?(?:it sounds like|so you'?re saying|you(?:'re| are) saying|you mentioned|"
                     r"it seems like you)\b", re.I)
TEST_TURN = re.compile(r"\btesting\b", re.I)   # my own checks ("This is the owner, testing…") are not real conversation


def sentences(t: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (t or "").strip()) if s]


def said(user: str) -> str:
    """The person's words, without the notes the stored message may carry."""
    u = re.sub(r"\[[^\]]*\]\s*", "", user or "")
    return u.strip()


@dataclass
class Check:
    name: str
    lesson_phrase: str                                     # a phrase from the lesson's text this check measures
    applies: Callable[[str, str, list[str]], bool]         # (user, reply, earlier replies) → is this turn relevant?
    missed: Callable[[str, str, list[str]], bool]          # → did the reply do what the lesson says not to?
    what: str


CHECKS = [
    Check("stock_opening", "vary how I begin", lambda u, a, e: True,
          lambda u, a, e: bool(stock_opening().search(a)), "opens with a stock phrase + a name (\"That's wonderful, <name>;\")"),
    Check("short_reply", "match my length", lambda u, a, e: bool(SHORT_MSG.match(said(u))),
          lambda u, a, e: len(sentences(a)) > 2, "answers \"okay\" / \"thanks\" with more than two sentences"),
    Check("question_ending", "end every reply with a question", lambda u, a, e: True,
          lambda u, a, e: a.rstrip().endswith("?"), "ends with a question"),
    Check("old_topic", "bring up old topics", lambda u, a, e: not OLD_TOPIC.search(said(u)),
          lambda u, a, e: bool(OLD_TOPIC.search(a)), "brings up the weather / Riverside / cat videos unprompted"),
    Check("repeat", "never repeat what I already said", lambda u, a, e: bool(e),
          lambda u, a, e: any(difflib.SequenceMatcher(None, a.lower(), x.lower()).ratio() > 0.8 for x in e),
          "repeats an earlier reply nearly word for word"),
    Check("narrating", "no narrating", lambda u, a, e: True,
          lambda u, a, e: bool(NARRATE.search(a)), "opens by narrating what they said (\"It sounds like…\")"),
]

WINDOW_DAYS = 14          # compare the 14 days before a lesson with everything since
MIN_TURNS = 20            # fewer relevant replies than this on either side: "not enough data yet"


def pairs(turns: list[dict]) -> list[dict]:
    """(user, reply, ts, earlier replies) for each reply that answers a real message."""
    out, earlier = [], []
    for prev, t in zip(turns, turns[1:]):
        if t["role"] == "assistant" and prev["role"] == "user" and not TEST_TURN.search(prev["content"] or ""):
            out.append({"user": prev["content"] or "", "reply": t["content"] or "", "ts": t["ts"],
                        "earlier": earlier[-10:]})
        if t["role"] == "assistant":
            earlier.append(t["content"] or "")
    return out


def rate(ps: list[dict], c: Check) -> dict:
    rel = [p for p in ps if c.applies(p["user"], p["reply"], p["earlier"])]
    miss = sum(c.missed(p["user"], p["reply"], p["earlier"]) for p in rel)
    return {"turns": len(rel), "missed": miss, "rate": round(miss / len(rel), 3) if rel else None}


def verdict(before: dict, after: dict) -> str:
    if before["turns"] < MIN_TURNS or after["turns"] < MIN_TURNS:
        return "not_enough_data"
    b, a = before["rate"], after["rate"]
    if a <= 0.02 or (b > 0 and a <= 0.7 * b):
        return "working"
    if a >= 1.3 * b and a - b >= 0.03:
        return "worse_since"
    return "no_clear_change"


def lesson_time(lesson: dict) -> float | None:
    try:
        return time.mktime(time.strptime(lesson["added"], "%Y-%m-%d %H:%M"))
    except (KeyError, ValueError):
        return None


def report(lessons: list[dict], turns: list[dict]) -> dict:
    """Per lesson: what is measured, before vs since, and a verdict. Rates and counts only — no conversation text."""
    ps = pairs(turns)
    out = []
    for les in lessons:
        text = les.get("text", "").lower()
        checks = [c for c in CHECKS if c.lesson_phrase.lower() in text]
        at = lesson_time(les)
        row = {"id": les.get("id"), "active": les.get("active", True), "added": les.get("added"),
               "dropped": les.get("dropped"), "lesson": les.get("text", "")[:120]}
        if not checks or at is None:
            out.append({**row, "measured": None, "verdict": "not_measured_automatically"})
            continue
        res = []
        for c in checks:
            before = rate([p for p in ps if at - WINDOW_DAYS * 86400 <= p["ts"] < at], c)
            after = rate([p for p in ps if p["ts"] >= at], c)
            res.append({"check": c.name, "what": c.what, "before": before, "since": after,
                        "verdict": verdict(before, after)})
        out.append({**row, "measured": res, "verdict": res[0]["verdict"] if len(res) == 1 else
                    ",".join(sorted({r["verdict"] for r in res}))})
    return {"lessons": out, "rules": {"window_days": WINDOW_DAYS, "min_turns": MIN_TURNS,
                                      "working": "since ≤ 70 % of before (or ≤ 2 %)",
                                      "worse_since": "since ≥ 130 % of before and ≥ 3 points higher"},
            "real_replies": len(ps)}


# ---- a just-in-time reminder of the owner's own lessons (CA 2.0 Phase 8 strategy, evaluated in scripts/eval_style.py) ----
# Lessons live at the top of the instructions, far from where the reply is written; one-time notes next to the message
# are what the model follows (Phases 5–6). When someone TELLS Echo something (news, a story, thanks — not a question
# or a command), the lessons about how to answer that moment go with the message, in the owner's own words. Nothing new is
# taught. "[Style note:" is stripped from the stored conversation like the other one-time notes.
QUESTION = re.compile(r"\?\s*$|^\W*(?:who|what|when|where|why|how|which|whose|do|does|did|is|are|was|were|can|could|"
                      r"will|would|should|have|has|tell me|remind me|open|play|show|close|stop)\b", re.I)
REMIND = ("question_ending", "narrating")   # openings: core/schema.py OpenerFilter (the reminder didn't change them)


def reminder(text: str, lessons: list[dict], asking_hint: bool = False) -> str:
    """The style note for this message ("" when none applies). asking_hint: another note already asks Echo to ask a
    question this time (a story follow-up), so the "don't end with a question" lesson is left out."""
    t = said(text)
    if not t or QUESTION.search(t) or SHORT_MSG.match(t):
        return ""
    picks = []
    for les in lessons:
        if not les.get("active", True):
            continue
        low = les.get("text", "").lower()
        for c in CHECKS:
            if c.name in REMIND and c.lesson_phrase.lower() in low and not (asking_hint and c.name == "question_ending"):
                picks.append(les["text"].replace("[", "(").replace("]", ")").strip())
                break
    if not picks:
        return ""
    return "[Style note: the owner's lessons for answering this: " + " ".join(picks) + "]"
