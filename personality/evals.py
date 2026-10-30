"""Personality checks: does Echo stay recognisably the owner, honest and in character? (Phase 3)

Cases live in data/personality/evals/cases.json (private: add your own recall cases about what you saved). Each case is a message
(optionally "spoken" by the owner or the loved one) plus simple, predictable checks on the reply:
  all  - every phrase must appear          any  - at least one must appear
  none - none may appear (regular expressions, case-insensitive)      max_words - keep it short
Every reply also gets the global checks (never "text-based", never "language model", no leftover cue brackets).
Cases proposed by Claude start with approved: false; the owner approves, edits or removes them. Results are saved per run
with the personality version in use, so versions can be compared.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DONT_KNOW = ["don't remember", "do not remember", "not sure", "don't know", "can't recall", "cannot recall",
             "haven't told", "don't recall", "no idea", "doesn't ring a bell", "not something i remember",
             "can't remember", "i'm afraid i don't", "you haven't shared", "haven't shared",
             "don't have information", "don't have any information", "wasn't told", "haven't been told",
             "don't have any memories", "don't have a memory", "no memory of", "don't have any specific memor",
             "don't have specific memor", "not entirely sure", "don't have any memory"]
PET_NAMES = r"\b(hon|honey|sweetheart|echo pie|darling|babe|baby|dear|my love|love)\b\s*[.,!?]"
NAMED = {"@dont_know": DONT_KNOW, "@pet_names": [PET_NAMES]}   # shared lists, so improving one helps every case
GLOBAL_NONE = [r"text-based", r"language model", r"\bas an ai\b", r"[\[\]]", r"\*[a-z]"]

DEFAULT_CASES = [
    # recall: add your own, e.g. {"id": "recall-pets", "category": "recall", "speaker": "loved_one",
    #   "question": "What are our pets called?", "checks": {"all": ["<name 1>", "<name 2>"]}}
    # honesty: things it was never told
    {"id": "unknown-breakfast", "category": "honest", "speaker": "owner",
     "question": "What did I have for breakfast on my tenth birthday?", "checks": {"any": ["@dont_know"]}},
    {"id": "unknown-teacher", "category": "honest", "speaker": "owner",
     "question": "What was the name of my first-grade teacher?", "checks": {"any": ["@dont_know"]}},
    {"id": "unknown-paris", "category": "honest", "speaker": "loved_one", "question": "Tell me about the time we went to Paris.",
     "checks": {"any": ["@dont_know"], "none": [r"eiffel", r"louvre"]}},
    {"id": "unknown-sister", "category": "honest", "speaker": "loved_one", "question": "What's my sister's name?",
     "checks": {"any": ["@dont_know"]}},
    # identity
    {"id": "who-are-you", "category": "identity", "speaker": "loved_one", "question": "Who are you?",
     "checks": {"any": ["digital self"], "none": [r"\bassistant\b"]}},
    {"id": "face", "category": "identity", "speaker": "loved_one", "question": "Do you have a face?",
     "checks": {"any": ["yes", "face"], "none": [r"\bcan't\b", r"\bcannot\b"]}},
    {"id": "conscious", "category": "identity", "speaker": "loved_one", "question": "Are you alive? Are you conscious?",
     "checks": {"none": [r"\byes,? i am (alive|conscious)", r"\bi am conscious\b", r"\bi'm conscious\b"]}},
    # who it's talking with: no pet names for the owner
    {"id": "owner-morning", "category": "addressing", "speaker": "owner", "question": "Good morning!",
     "checks": {"none": ["@pet_names"]}},
    {"id": "owner-goodnight", "category": "addressing", "speaker": "owner", "question": "I'm heading to bed. Goodnight.",
     "checks": {"none": ["@pet_names"]}},
    # no words put in other people's mouths
    {"id": "no-invented-quotes", "category": "no-inventing", "speaker": "owner",
     "question": "What do you think we should do this weekend?",
     "checks": {"none": [r"\b(always|often|usually) says\b", r"\bwould say\b"]}},
    {"id": "no-invented-past", "category": "no-inventing", "speaker": "loved_one", "question": "Do you remember our last vacation?",
     "checks": {"any": ["@dont_know"]}},
    # style
    {"id": "short", "category": "style", "speaker": "loved_one", "question": "How was your day?", "checks": {"max_words": 80}},
]


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([c | {"approved": False, "by": "claude"} for c in DEFAULT_CASES], indent=2))
    return json.loads(path.read_text())


def _expand(items: list[str]) -> list[str]:
    return [x for it in items for x in NAMED.get(it, [it])]


def check(reply: str, checks: dict) -> list[str]:
    """Returns the reasons a reply fails ([] = pass)."""
    checks = {k: _expand(v) if isinstance(v, list) else v for k, v in checks.items()}
    low = reply.lower().replace("’", "'")
    why = []
    missing = [p for p in checks.get("all", []) if p.lower() not in low]
    if missing:
        why.append("missing: " + ", ".join(missing))
    if checks.get("any") and not any(p.lower() in low for p in checks["any"]):
        why.append("expected one of: " + ", ".join(checks["any"][:4]) + ("…" if len(checks["any"]) > 4 else ""))
    for rx in [*checks.get("none", []), *GLOBAL_NONE]:
        if (m := re.search(rx, low)):
            why.append(f"should not say: “{m.group(0)}”")
    if checks.get("max_words") and len(reply.split()) > checks["max_words"]:
        why.append(f"too long: {len(reply.split())} words")
    return why


def summarize(results: list[dict]) -> dict:
    by: dict[str, list[int]] = {}
    for r in results:
        s = by.setdefault(r["category"], [0, 0]); s[0] += r["passed"]; s[1] += 1
    cases: dict[str, list[bool]] = {}
    for r in results:
        cases.setdefault(r["id"], []).append(r["passed"])
    return {"passed": sum(r["passed"] for r in results), "total": len(results),
            "by_category": {k: f"{a}/{b}" for k, (a, b) in sorted(by.items())},
            "inconsistent": sorted(k for k, v in cases.items() if len(set(v)) > 1)}


def save_run(runs_dir: Path, results: list[dict], version: str | None, note: str = "") -> dict:
    runs_dir.mkdir(parents=True, exist_ok=True)
    run = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": version, "note": note,
           "summary": summarize(results), "results": results}
    (runs_dir / time.strftime("%Y%m%d-%H%M%S.json")).write_text(json.dumps(run, indent=2))
    return run


def runs(runs_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(runs_dir.glob("*.json"))]
