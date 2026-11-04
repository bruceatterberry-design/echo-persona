"""Memory export (BRIDGE_PROTOCOL.md section 6): bounded, read-only batches from Echo's memory for the tower.

- Opens Echo's databases READ-ONLY (sqlite URI mode=ro). Never writes to Echo.
- Off until the owner says "all systems go" (data/dream-bridge/export.json {"enabled": true}).
- Never sends: hidden memories, classroom/student data (FERPA), audio, photos (only their text
  descriptions), face/voice data, sources/file paths, secrets.
- Rotates: memories that are new or changed since they were last sent go first, then the longest-unsent ones,
  so dream cycles don't repeat the same batch.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import store

MAX_MEMORIES = 60
PRIVATE_WORDS = re.compile(r"\b(students?|pupils?|ieps?|classrooms?|lesson plans?|ferpa|infinite campus|teacher tools?)\b", re.I)


def _db(name: str) -> Path:
    return store.ECHO_DIR / "data" / name


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def private(*texts: str | None) -> bool:
    return any(t and PRIVATE_WORDS.search(t) for t in texts)


# ---- switch + rotation state (all in the bridge's own folder, not Echo's) ----
def settings() -> dict:
    p = store.STATE_DIR / "export.json"
    return json.loads(p.read_text()) if p.exists() else {"enabled": False}


def set_enabled(on: bool, by: str = "owner") -> dict:
    s = {**settings(), "enabled": on, "changed_at": time.time(), "changed_by": by}
    store._write_private(store.STATE_DIR / "export.json", s)
    store.journal(event="export_" + ("on" if on else "off"), by=by)
    return s


def sent_log() -> dict:
    p = store.STATE_DIR / "sent.json"
    return json.loads(p.read_text()) if p.exists() else {"memories": {}, "batches": []}


def mark_sent(batch: dict) -> None:
    log = sent_log()
    now = time.time()
    for m in batch["memories"]:
        log["memories"][str(m["id"])] = now
    log["batches"] = (log["batches"] + [{"batch_id": batch["batch_id"], "at": now, "n": len(batch["memories"])}])[-200:]
    store._write_private(store.STATE_DIR / "sent.json", log)


# ---- building a batch ----
def _eligible(con: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
    rows = con.execute("SELECT id, kind, content, status, confidence, year, when_text, people, place, told_by, updated "
                       "FROM memories WHERE hidden IS NULL").fetchall()
    keep = [r for r in rows if not private(r["content"], r["people"], r["place"])]
    return keep, len(rows) - len(keep)


def _choose(rows: list[sqlite3.Row], sent: dict, limit: int) -> list[sqlite3.Row]:
    def fresh(r):   # never sent, or changed since last sent
        last = sent.get(str(r["id"]))
        return last is None or r["updated"] > last
    new = sorted((r for r in rows if fresh(r)), key=lambda r: -r["updated"])
    old = sorted((r for r in rows if not fresh(r)), key=lambda r: sent.get(str(r["id"]), 0))
    return (new + old)[:limit]


def _search(con: sqlite3.Connection, query: str, rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    words = [w for w in re.findall(r"[A-Za-z0-9']{2,}", query)][:12]
    if not words:
        return []
    allowed = {r["id"]: r for r in rows}
    expr = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
    try:
        hits = con.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                           (expr, limit * 3)).fetchall()
    except sqlite3.Error:
        return []
    return [allowed[h[0]] for h in hits if h[0] in allowed][:limit]


def build(query: str | None = None, refs: list[str] | None = None, limit: int = MAX_MEMORIES,
          previous_dream_ids: list[str] | None = None) -> dict:
    """One memory_batch payload. Doesn't mark anything as sent (the caller does after delivery)."""
    limit = max(1, min(int(limit or MAX_MEMORIES), MAX_MEMORIES))
    with _ro(_db("memory.sqlite")) as con:
        rows, held_back = _eligible(con)
        if refs:
            want = {int(r.split(":", 1)[1]) for r in refs if r.startswith("memory:") and r.split(":", 1)[1].isdigit()}
            chosen = [r for r in rows if r["id"] in want][:limit]
        elif query:   # a search, topped up with the normal rotation if it finds little (the tower's standard
            chosen = _search(con, query, rows, limit)   # request is a generic phrase, TOWER_TO_MAC_2)
            if len(chosen) < min(10, limit):
                have = {r["id"] for r in chosen}
                chosen += [r for r in _choose(rows, sent_log()["memories"], limit) if r["id"] not in have][:limit - len(chosen)]
        else:
            chosen = _choose(rows, sent_log()["memories"], limit)
        ids = [r["id"] for r in chosen]
        q = ",".join("?" * len(ids)) or "NULL"
        rel = con.execute(f"SELECT a_type, a_id, rel, b_type, b_id, weight FROM relations WHERE "
                          f"(a_type='memory' AND a_id IN ({q})) OR (b_type='memory' AND b_id IN ({q}))", ids + ids).fetchall()
        ent_ids = {r["b_id"] for r in rel if r["b_type"] == "entity"} | {r["a_id"] for r in rel if r["a_type"] == "entity"}
        facts = con.execute(f"SELECT id, subject_id, predicate, object_id, value, status, confidence FROM facts "
                            f"WHERE memory_id IN ({q})", ids).fetchall()
        facts = [f for f in facts if not private(f["value"], f["predicate"])]
        for f in facts:
            ent_ids.add(f["subject_id"])
            if f["object_id"]:
                ent_ids.add(f["object_id"])
        eq = ",".join("?" * len(ent_ids)) or "NULL"
        ents = con.execute(f"SELECT id, type, name, note FROM entities WHERE id IN ({eq})", list(ent_ids)).fetchall()
        ents = [e for e in ents if not private(e["name"], e["note"])]
    ok_ent = {e["id"] for e in ents}
    ok_mem = set(ids)

    def ref_ok(t, i):
        return (t == "memory" and i in ok_mem) or (t == "entity" and i in ok_ent)

    batch = {
        "batch_id": str(uuid.uuid4()),
        "memories": [{"id": r["id"], "kind": r["kind"], "content": r["content"], "confidence": r["confidence"],
                      "year": r["year"], "when_text": r["when_text"], "people": r["people"], "place": r["place"],
                      "told_by": r["told_by"], "updated": _iso(r["updated"])} for r in chosen],
        "entities": [dict(e) for e in ents],
        "facts": [dict(f) for f in facts if f["subject_id"] in ok_ent and (not f["object_id"] or f["object_id"] in ok_ent)],
        "relations": [{"a": f"{r['a_type']}:{r['a_id']}", "rel": r["rel"], "b": f"{r['b_type']}:{r['b_id']}", "weight": r["weight"]}
                      for r in rel if ref_ok(r["a_type"], r["a_id"]) and ref_ok(r["b_type"], r["b_id"])],
        "episodes": [],          # Echo's episodes have no summaries yet; the "summary" memories carry them
        "open_questions": open_questions(),
        "interests": interests(),
        "previous_dream_ids": previous_dream_ids or [],
    }
    batch["_held_back"] = held_back   # removed before sending; shown in previews
    return batch


def open_questions() -> list[str]:
    p = _db("learning.sqlite")
    if not p.exists():
        return []
    with _ro(p) as con:
        rows = con.execute("SELECT question FROM gaps WHERE status='open' ORDER BY last DESC LIMIT 20").fetchall()
    return [r[0].strip() for r in rows if "?" in r[0] and len(r[0]) <= 300 and not private(r[0])]


def interests() -> list[str]:
    p = store.ECHO_DIR / "config" / "music.json"
    try:
        m = json.loads(p.read_text())
    except Exception:
        return []
    out = [f"music genre: {g}" for g in m.get("approved_genres", []) if isinstance(g, str)]
    out += [f"music artist: {a}" for a in m.get("approved_artists", []) if isinstance(a, str)]
    return out[:30]


def for_sending(batch: dict) -> dict:
    return {k: v for k, v in batch.items() if not k.startswith("_")}


def save_outbox(batch: dict, name: str) -> Path:
    p = store.dirs()["outbox"] / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}-{batch['batch_id']}.json"
    p.write_text(json.dumps(batch, indent=1, ensure_ascii=False))
    return p


def summary(batch: dict) -> dict:
    kinds: dict[str, int] = {}
    for m in batch["memories"]:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    return {"batch_id": batch["batch_id"], "memories": len(batch["memories"]), "by_kind": kinds,
            "entities": len(batch["entities"]), "facts": len(batch["facts"]), "relations": len(batch["relations"]),
            "open_questions": len(batch["open_questions"]), "interests": len(batch["interests"]),
            "held_back_private": batch.get("_held_back", 0),
            "bytes": len(json.dumps(for_sending(batch), ensure_ascii=False).encode())}
