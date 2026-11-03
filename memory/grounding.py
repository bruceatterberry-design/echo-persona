"""Metacognition, Stage 4a (approved order, the owner 2026-11-01): how well is an answer grounded in Echo's memory?

No LLM call: the label comes from what retrieval (memory/retrieve.py) found for the message and why.

    known     a CONFIRMED memory (confidence >= 0.8) that the message clearly points to (fact / about / 2+ shared words /
              meaning similarity >= 0.75)
    believed  clearly pointed to, but only Echo's own notes or less certain memories
    inferred  only loose links (meaning < 0.75 alone, same person + topic, the time asked about, or just general facts
              about a person named)
    unknown   a memory question (controller kind "memory") and nothing was found → Echo should say it doesn't know
    none      not a memory question and nothing relevant: the answer comes from general knowledge / reasoning

Stage 4b: note(label) gives the one-time note core/server.py adds to the message, for "unknown" only
(stripped from history by server.strip_hints, like every "[...]" note). Stage 4a only RECORDED the label (core/workspace.py); nothing acts on it yet. scripts/bench_grounding.py checks the
labels against questions whose right answers are known, so "known" is measured, not self-reported.
The 0.75 meaning cut and "facts alone = inferred" were chosen on the odd-numbered questions and checked on the
even-numbered ones (docs/cognitive/STAGE_4A_REPORT.md): held-out claims 29/29 right, 5/5 no-answer questions not claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STRONG = ("fact", "about", "words")   # reason prefixes from memory/retrieve.py Hit.why ("meaning 0.71" handled apart)
MEANING_SURE = 0.75
WEAK = ("with", "time")
LABELS = ("known", "believed", "inferred", "unknown", "none")
SURE = 0.8


@dataclass
class Grounding:
    label: str
    basis: list[str] = field(default_factory=list)   # reason kinds only (no names, no text)
    top_id: int | None = None

    def as_record(self) -> dict:
        return {"label": self.label, "basis": self.basis, **({"top_id": self.top_id} if self.top_id else {})}


def _kinds(why: list[str]) -> list[str]:
    return sorted({w.split()[0] for w in why})


def hit_label(hit) -> Grounding:
    """known / believed / inferred for one retrieval hit (label() uses the top one; Stage 5c checks every new one)."""
    ks = _kinds(hit.why)
    sim = max([float(w.split()[1]) for w in hit.why if w.startswith("meaning ")] or [0.0])
    strong = any(k in STRONG for k in ks) or sim >= MEANING_SURE
    m = hit.memory
    sure = getattr(m, "status", None) == "confirmed" and (getattr(m, "confidence", 0) or 0) >= SURE
    return Grounding("known" if strong and sure else "believed" if strong else "inferred", ks, m.id)


def label(res, kind: str | None = None) -> Grounding:
    """res = memory.retrieve.Result (or None if retrieval didn't run); kind = controller decision kind."""
    hits = list(getattr(res, "hits", None) or [])
    facts = getattr(res, "facts", None) or {}
    if hits:
        return hit_label(hits[0])
    if facts:
        return Grounding("inferred", ["facts"])
    if kind == "memory":
        return Grounding("unknown")
    return Grounding("none")


UNKNOWN_NOTE = ("[My memory check: I found nothing in my memories about this. If it's about my life, people we know or "
                "our past, I say plainly that I don't remember it or wasn't told, and I don't make up names, dates or "
                "details. I can ask them to tell me about it.]")
# Tried and switched OFF (the owner chose 2026-11-01, docs/cognitive/STAGE_4B_REPORT.md): mixed effect, awkward
# "If I remember right…" on fine answers, +0.2 s first word. Kept for reference, not used.
INFERRED_NOTE = ("[My memory check: my notes only loosely match this. Whatever I say from memory I put as \"I think…\" or "
                 "\"if I remember right…\", and I don't add details that aren't in my notes.]")


def note(label_: str | None) -> str:
    """One-time note for the reply: only when memory gives nothing solid. known / believed / none → no note."""
    return UNKNOWN_NOTE if label_ == "unknown" else ""
