"""Dream links (Echo Dream Engine, 2026-11-03): connections between memories Echo ALREADY has, made by a dream and
approved by the owner. They add understanding, never memories: no memory, fact or person is created or changed here.

- Own table (dream_links). memory/consolidate.py rebuilds the 'related'/'repeats' rows in `relations` on every run,
  so dream links can't live there without being wiped.
- Fading: a link's strength halves every HALF_LIFE_DAYS since it was last used, and it goes quiet below QUIET_BELOW.
  Being used (Echo recalled it in a conversation) keeps it alive, so only useful connections last.
- Recall (memory/retrieve.py): at most ONE linked memory joins the notes, only when there is room (never pushing out a
  real match), labelled as a link Echo made while reflecting.
- Every change is recorded in `versions` (item_type 'dream_link').
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_links (   -- connections between existing memories, from approved dreams (memory/dreamlinks.py)
  a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,   -- memory ids, a_id < b_id
  weight REAL NOT NULL, created REAL NOT NULL, last_used REAL, uses INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',   -- active | removed
  PRIMARY KEY(a_id, b_id)
);
CREATE INDEX IF NOT EXISTS dream_links_b ON dream_links(b_id);
"""
START_WEIGHT = 0.5       # weaker than anything Echo was told
HALF_LIFE_DAYS = 30
QUIET_BELOW = 0.2
MAX_LINKS = 500          # keeps the table (and lookups) small


def effective(weight: float, created: float, last_used: float | None, now: float | None = None) -> float:
    age_days = ((now or time.time()) - (last_used or created)) / 86400
    return weight * 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def add(db: sqlite3.Connection, a: int, b: int, source: str, by: str = "owner:dream", now: float | None = None) -> dict:
    """Link two existing, visible memories. Raises ValueError if either is missing or hidden."""
    if a == b:
        raise ValueError("a memory can't link to itself")
    a, b = _pair(int(a), int(b))
    rows = db.execute("SELECT id FROM memories WHERE id IN (?, ?) AND hidden IS NULL", (a, b)).fetchall()
    if len(rows) != 2:
        raise ValueError("both memories must exist and not be hidden")
    now = now or time.time()
    old = db.execute("SELECT weight, status, source FROM dream_links WHERE a_id=? AND b_id=?", (a, b)).fetchone()
    if not old:
        n = db.execute("SELECT count(*) FROM dream_links WHERE status='active'").fetchone()[0]
        if n >= MAX_LINKS:
            raise OverflowError(f"Echo already has {MAX_LINKS} dream links")
    with db:
        if old:   # a second dream found the same link: renew it rather than duplicate it
            db.execute("UPDATE dream_links SET weight=?, status='active', last_used=?, source=? WHERE a_id=? AND b_id=?",
                       (max(START_WEIGHT, old[0]), now, f"{old[2]};{source}"[:500], a, b))
        else:
            db.execute("INSERT INTO dream_links (a_id, b_id, weight, created, source) VALUES (?,?,?,?,?)",
                       (a, b, START_WEIGHT, now, source[:500]))
        db.execute("INSERT INTO versions (item_type, item_id, action, before, after, by, at, reason) VALUES "
                   "('dream_link', ?, ?, ?, ?, ?, ?, ?)",
                   (a, "edit" if old else "create", json.dumps({"a": a, "b": b, "weight": old[0], "status": old[1]}) if old else None,
                    json.dumps({"a": a, "b": b, "weight": START_WEIGHT}), by, now, source[:500]))
    return {"a": a, "b": b, "weight": START_WEIGHT, "renewed": bool(old)}


def remove(db: sqlite3.Connection, a: int, b: int, by: str = "owner") -> bool:
    a, b = _pair(int(a), int(b))
    with db:
        n = db.execute("UPDATE dream_links SET status='removed' WHERE a_id=? AND b_id=? AND status='active'", (a, b)).rowcount
        if n:
            db.execute("INSERT INTO versions (item_type, item_id, action, before, after, by, at, reason) VALUES "
                       "('dream_link', ?, 'hide', ?, ?, ?, ?, 'removed')",
                       (a, json.dumps({"a": a, "b": b, "status": "active"}), json.dumps({"a": a, "b": b, "status": "removed"}),
                        by, time.time()))
    return bool(n)


def neighbours(db: sqlite3.Connection, ids: list[int], now: float | None = None) -> list[tuple[int, int, float]]:
    """(from_id, linked_id, strength) for active, not-faded links of these memories, strongest first."""
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    try:
        rows = db.execute(f"SELECT a_id, b_id, weight, created, last_used FROM dream_links WHERE status='active' "
                          f"AND (a_id IN ({q}) OR b_id IN ({q}))", list(ids) + list(ids)).fetchall()
    except sqlite3.OperationalError:   # table not there (old database): no links
        return []
    out = []
    for a, b, w, c, lu in rows:
        s = effective(w, c, lu, now)
        if s >= QUIET_BELOW:
            out.append((a, b, s) if a in ids else (b, a, s))
    return sorted(out, key=lambda x: -x[2])


def used(db: sqlite3.Connection, a: int, b: int, now: float | None = None) -> None:
    a, b = _pair(a, b)
    with db:
        db.execute("UPDATE dream_links SET last_used=?, uses=uses+1 WHERE a_id=? AND b_id=?", (now or time.time(), a, b))


def listing(db: sqlite3.Connection, now: float | None = None) -> list[dict]:
    try:
        rows = db.execute("SELECT a_id, b_id, weight, created, last_used, uses, source, status FROM dream_links "
                          "ORDER BY created DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"a": a, "b": b, "strength": round(effective(w, c, lu, now), 3), "uses": u, "source": s, "status": st,
             "quiet": effective(w, c, lu, now) < QUIET_BELOW} for a, b, w, c, lu, u, s, st in rows]
