"""Cognitive compression, Stage 6a (directive part 7): spot procedures Echo repeats, as SKILL CANDIDATES. Report only.

Source: the per-turn records (core/workspace.py, 30 days, no message text). A procedure = the controller's task kind
plus the tools used in order (e.g. research: web_search → read_webpage). For each one: how often, how often it
succeeded (every tool ok, no LLM error), LLM calls per turn and times.

A candidate = seen at least MIN_COUNT times with at least MIN_SUCCESS success. `could_shortcut` marks ones where the
model spent an extra LLM round deciding which tools to call (llm_calls > 1): a validated shortcut (like the hand-written
tools.prefetch_calls) could run those tools first and save that round. Nothing is executed or changed here; a
shortcut would be a later stage, tried in shadow mode and validated before Echo uses it (directive: "must pass
validation before becoming a preferred execution path").

Already compressed by hand, so not candidates: commands handled without the LLM (core/browser.py, core/music.py) and
tools.prefetch_calls (addresses and searches in the message are fetched before the model starts).
"""
from __future__ import annotations

import statistics

MIN_COUNT = 5
MIN_SUCCESS = 0.8


def signature(rec: dict) -> str | None:
    """'research: web_search → read_webpage' / 'memory: (no tools)'; None for turns that aren't procedures."""
    route = rec.get("route") or ""
    if not route.startswith("llm"):
        return None
    kind = (rec.get("plan") or {}).get("kind", "?")
    tools = [t.split(":")[0] for t in rec.get("tools") or []]
    return f"{kind}: " + (" → ".join(tools) if tools else "(no tools)")


def ok(rec: dict) -> bool:
    return rec.get("route") != "llm_error" and all(t.endswith(":ok") for t in rec.get("tools") or [])


def procedures(records: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in records:
        if (sig := signature(r)):
            groups.setdefault(sig, []).append(r)
    out = []
    for sig, rs in groups.items():
        n, good = len(rs), sum(ok(r) for r in rs)
        calls = [r.get("llm_calls") or 1 for r in rs]
        first = [r["first_ms"] for r in rs if r.get("first_ms") is not None]
        total = [r["total_ms"] for r in rs if r.get("total_ms") is not None]
        tooly = "(no tools)" not in sig
        out.append({"procedure": sig, "count": n, "success": f"{good}/{n}",
                    "llm_calls_mean": round(statistics.mean(calls), 2),
                    "first_ms_median": statistics.median(first) if first else None,
                    "total_ms_median": statistics.median(total) if total else None,
                    "candidate": tooly and n >= MIN_COUNT and good / n >= MIN_SUCCESS,
                    "could_shortcut": tooly and statistics.mean(calls) > 1})
    return sorted(out, key=lambda p: (-p["candidate"], -p["count"]))


def report(records: list[dict]) -> dict:
    procs = procedures(records)
    return {"turns": len(records), "procedures": procs, "candidates": [p for p in procs if p["candidate"]],
            "rules": {"min_count": MIN_COUNT, "min_success": MIN_SUCCESS}}


# ---- Stage 6b (the owner OK'd 2026-11-01): "pick the link" shortcut, SHADOW MODE ----
# After page_links, the model spends a whole LLM round choosing which link to read (median 22 s procedure, 2 rounds).
# The shortcut ranks the same links by the user's own words. Now it is only compared with the model's choice
# (tools.Turn.shadow → turn record "skill_shadow"); it would only be switched on if it agrees ≥ 90% and saves time.
import re as _re

_STOP = set("the a an and or of to in on at for with about from by is are was were be this that it its into as what "
            "which who how why when where please tell me find follow links link article page start go read".split())


def _w(t: str) -> set[str]:
    return {w for w in _re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) > 2 and w not in _STOP}


def rank_links(question: str, links: list) -> list:
    """Links that share words with the question, best first. A word counts more the rarer it is among this page's links
    (log N/df): "json" beats "function" on a page where most links say "function" (2026-11-01 shadow test)."""
    import math
    want = _w(_re.sub(r"\S*(?:https?://|www\.)\S*|\S+\.(?:com|org|net|gov|edu)\S*", " ", question or ""))   # not the address
    bags = [_w(getattr(ln, "text", "") + " " + _re.sub(r"^https?://[^/]+", "", getattr(ln, "url", "")).replace("_", " "))
            for ln in links]
    n = max(1, len(bags))
    df: dict[str, int] = {}
    for b in bags:
        for w in b & want:
            df[w] = df.get(w, 0) + 1
    scored = [(sum(math.log(1 + n / df[w]) for w in b & want), i, ln) for i, (b, ln) in enumerate(zip(bags, links))]
    return [ln for s, i, ln in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0]


def pick_link(question: str, links: list):
    r = rank_links(question, links)
    return r[0] if r else None
