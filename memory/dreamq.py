"""Questions from Echo's dreams (the owner, 2026-11-04: "dreaming is about refinement, clearing out the unknown variables").

The Dream Bridge (separate service, ~/Echo-dream/dream_bridge, bridge/autodecide.py) lets Echo's own model review
each dream "question"; ones he agrees are real gaps are reworded as one friendly question into the bridge's
data/dream-bridge/proposals.sqlite, table dream_questions. Here we only READ that table, so curiosity can ask them:
same rules as every curiosity question (the owner's voice only, rate-limited, answer -> suggestion on My memories,
nothing kept without review). Missing file/table = no dream questions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "dream-bridge" / "proposals.sqlite"
PREFIX = "dream:"


def questions(db: Path | None = None, limit: int = 5) -> list[dict]:
    p = db or DB
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute("SELECT proposal_id, text FROM dream_questions ORDER BY queued LIMIT ?", (limit * 4,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [{"id": PREFIX + pid, "text": text, "period": None, "source": "dream"} for pid, text in rows if text][:limit * 4]
