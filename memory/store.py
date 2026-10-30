"""Durable local memory (SQLite). Nothing is saved unless the owner asks or approves.

Two layers:
  * memories: durable facts/preferences, each confirmed or inferred, with source + confidence.
  * turns:    recent conversation log, so short-term context survives a restart.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["fact", "preference", "person", "project", "correction", "behavior", "summary", "memory"]
Status = Literal["confirmed", "inferred"]
# "memory" = a life memory (an experience or event). Life period helps Echo tell childhood from recent life.
Period = Literal["childhood", "teen", "young_adult", "adult", "recent", "unknown"]
LIFE_FIELDS = ("period", "when_text", "year", "people", "place", "told_by")
# Columns added after the first version (v2: life memories). Older databases get them on open, after a backup copy.
V2_COLUMNS = {"period": "TEXT", "when_text": "TEXT", "year": "INTEGER", "people": "TEXT", "place": "TEXT", "told_by": "TEXT"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  source TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content); END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vectors (   -- meaning of each memory (memory/embed.py), for search by meaning
  memory_id INTEGER PRIMARY KEY,
  model TEXT NOT NULL,
  updated REAL NOT NULL,       -- the memory's "updated" when this was made; older → made again
  vec BLOB NOT NULL            -- float32, unit length
);
CREATE TABLE IF NOT EXISTS candidates (   -- suggested life memories from the owner's recordings, waiting for his review
  id INTEGER PRIMARY KEY,
  content TEXT NOT NULL,
  period TEXT, when_text TEXT, year INTEGER, people TEXT, place TEXT,
  quote TEXT NOT NULL,          -- the owner's exact words that support it
  source TEXT NOT NULL,         -- the recording (relative to the dataset root)
  question TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  memory_id INTEGER,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY,
  a INTEGER NOT NULL,            -- the memory kept earlier
  b INTEGER NOT NULL,            -- the newer memory that may disagree with it
  score REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',   -- open | kept_both | resolved
  created REAL NOT NULL,
  UNIQUE(a, b)
);
"""


class Memory(BaseModel):
    id: int | None = None
    kind: Kind = "fact"
    content: str = Field(min_length=1, max_length=2000)
    status: Status = "confirmed"
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    source: str = Field("owner:chat", max_length=200)
    created: float | None = None
    updated: float | None = None
    # optional, mostly for kind "memory"
    period: Period | None = None
    when_text: str | None = Field(None, max_length=60)      # "summer 1974", "age 8", "last Tuesday"
    year: int | None = Field(None, ge=1900, le=2200)
    people: str | None = Field(None, max_length=300)        # comma-separated names
    place: str | None = Field(None, max_length=200)
    told_by: str | None = Field(None, max_length=40)        # who said it: "owner", "loved_one", …


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")))


def similarity(a: str, b: str) -> float:
    """Word-overlap (Jaccard) of two texts, 0..1. Cheap and predictable; good enough to catch repeats."""
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


DUPLICATE_AT = 0.85
# Words that say little about what a memory is *about*. Negations (not/never/no) are kept on purpose.
STOP = set("a an the and or but of to in on at for with is was were are be been am i my me we our it its that this "
           "there their they he she his her you your as by from so".split())


def topic_similarity(a: str, b: str) -> float:
    """Like similarity(), but on meaningful words only: how much two memories are about the same thing."""
    wa, wb = _words(a) - STOP, _words(b) - STOP
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


RELATED_AT = 0.3      # about the same thing → worth checking for a contradiction
FLAG_WITHOUT_JUDGE = 0.5   # if no language model can judge, flag only closely related pairs


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        have = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
        missing = {c: t for c, t in V2_COLUMNS.items() if c not in have}
        if not missing:
            return
        if self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]:   # real data: keep a copy first
            self.backup(self.path.with_name(f"{self.path.stem}-before-v2-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"))
        with self.db:
            for c, t in missing.items():
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {c} {t}")

    def close(self) -> None:
        self.db.close()

    # ---- durable memories ----
    def save(self, m: Memory) -> Memory:
        return self.add(m)[0]

    def add(self, m: Memory) -> tuple[Memory, bool]:
        """Save a memory unless it's a near-repeat of one already kept. Returns (memory, was_duplicate).
        A repeat never creates a second copy; if the new one is confirmed and the old one only inferred,
        the old one is upgraded (and missing life details are filled in). Nothing is ever downgraded."""
        dup = self.find_duplicate(m.content)
        if dup:
            upd = {f: getattr(m, f) for f in LIFE_FIELDS if getattr(dup, f) is None and getattr(m, f) is not None}
            if m.status == "confirmed" and dup.status == "inferred":
                upd |= {"status": "confirmed", "confidence": max(m.confidence, dup.confidence), "source": m.source}
            return (self.update(dup.id, **upd) if upd else dup), True
        now = time.time()
        cols = ["kind", "content", "status", "confidence", "source", "created", "updated", *LIFE_FIELDS]
        vals = [m.kind, m.content.strip(), m.status, m.confidence, m.source, now, now, *(getattr(m, f) for f in LIFE_FIELDS)]
        with self.db:
            cur = self.db.execute(f"INSERT INTO memories({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return self.get(cur.lastrowid), False

    def find_duplicate(self, content: str) -> Memory | None:
        best, score = None, 0.0
        for c in self.search(content, limit=20):
            s = similarity(content, c.content)
            if s > score:
                best, score = c, s
        return best if score >= DUPLICATE_AT else None

    def get(self, mid: int) -> Memory | None:
        r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return Memory(**dict(r)) if r else None

    def all(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories ORDER BY id")]

    def update(self, mid: int, **fields) -> Memory | None:
        cur = self.get(mid)
        if not cur:
            return None
        new = cur.model_copy(update={k: v for k, v in fields.items() if v is not None})
        Memory.model_validate(new.model_dump())  # re-validate
        with self.db:
            self.db.execute(
                "UPDATE memories SET kind=?,content=?,status=?,confidence=?,source=?,updated=?,"
                + ",".join(f"{f}=?" for f in LIFE_FIELDS) + " WHERE id=?",
                (new.kind, new.content.strip(), new.status, new.confidence, new.source, time.time(),
                 *(getattr(new, f) for f in LIFE_FIELDS), mid))
        return self.get(mid)

    def correct(self, mid: int, content: str) -> Memory | None:
        """The owner corrected this memory: it becomes confirmed at full confidence."""
        return self.update(mid, content=content, status="confirmed", confidence=1.0, source="owner:correction")

    def forget(self, mid: int) -> bool:
        with self.db:
            self.db.execute("DELETE FROM conflicts WHERE a=? OR b=?", (mid, mid))
            self.db.execute("DELETE FROM vectors WHERE memory_id=?", (mid,))
            return self.db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount > 0

    # ---- suggested memories (memory/candidates.py) → the owner approves → real memories ----
    CAND_FIELDS = ("content", "period", "when_text", "year", "people", "place", "quote", "source", "question")

    def add_candidate(self, c: dict) -> int | None:
        """Keep a suggestion unless it repeats a memory or another suggestion. Returns its id (None if skipped)."""
        text = str(c.get("content", "")).strip()
        if not text or self.find_duplicate(text):
            return None
        for r in self.db.execute("SELECT content FROM candidates WHERE status != 'rejected' OR source = ?", (c.get("source"),)):
            if similarity(text, r[0]) >= DUPLICATE_AT:
                return None
        with self.db:
            cur = self.db.execute(
                f"INSERT INTO candidates({','.join(self.CAND_FIELDS)},created) VALUES ({','.join('?' * len(self.CAND_FIELDS))},?)",
                (*(c.get(k) for k in self.CAND_FIELDS), time.time()))
        return cur.lastrowid

    def candidates(self, status: str = "pending") -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM candidates WHERE status=? ORDER BY id", (status,))]

    def candidate(self, cid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def approve_candidate(self, cid: int, edits: dict | None = None) -> tuple[Memory, bool] | None:
        """the owner approved it (maybe edited): it becomes a confirmed life memory that remembers its recording."""
        c = self.candidate(cid)
        if not c or c["status"] != "pending":
            return None
        c |= {k: v for k, v in (edits or {}).items() if k in self.CAND_FIELDS and k not in ("quote", "source")}
        m, dup = self.add(Memory(kind="memory", content=c["content"], status="confirmed", confidence=1.0,
                                 source=f"owner:recording:{c['source']}"[:200], told_by="owner",
                                 **{k: (c[k] or None) for k in ("period", "when_text", "year", "people", "place")}))
        with self.db:
            self.db.execute("UPDATE candidates SET status='approved', memory_id=? WHERE id=?", (m.id, cid))
        return m, dup

    def reject_candidate(self, cid: int) -> bool:
        with self.db:
            return self.db.execute("UPDATE candidates SET status='rejected' WHERE id=? AND status='pending'", (cid,)).rowcount > 0

    # ---- search by meaning (vectors made by memory/embed.py) ----
    def needs_vectors(self, model: str, limit: int = 64) -> list[Memory]:
        """Memories with no vector yet, an outdated one (edited since) or one from another model."""
        rows = self.db.execute(
            "SELECT m.* FROM memories m LEFT JOIN vectors v ON v.memory_id=m.id "
            "WHERE v.memory_id IS NULL OR v.updated < m.updated OR v.model != ? ORDER BY m.id LIMIT ?", (model, limit))
        return [Memory(**dict(r)) for r in rows]

    def set_vector(self, m: Memory, model: str, vec) -> None:
        import numpy as np
        v = np.asarray(vec, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        with self.db:
            self.db.execute("INSERT INTO vectors(memory_id,model,updated,vec) VALUES (?,?,?,?) ON CONFLICT(memory_id) "
                            "DO UPDATE SET model=excluded.model, updated=excluded.updated, vec=excluded.vec",
                            (m.id, model, m.updated, v.tobytes()))

    def by_meaning(self, qvec, model: str, limit: int = 8, min_sim: float = 0.0) -> list[tuple[Memory, float]]:
        """Closest memories to a query vector (cosine), best first. Brute force: fine for tens of thousands."""
        import numpy as np
        rows = self.db.execute("SELECT memory_id, vec FROM vectors WHERE model=?", (model,)).fetchall()
        if not rows:
            return []
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1) @ q
        out = []
        for i in np.argsort(-sims)[:limit]:
            if sims[i] < min_sim:
                break
            if (m := self.get(rows[i][0])):
                out.append((m, float(sims[i])))
        return out

    # ---- possible contradictions (never resolved automatically) ----
    def related(self, m: Memory, limit: int = 8) -> list[tuple[Memory, float]]:
        """Kept memories about the same thing as m (not repeats, not conversation notes), closest first."""
        out = []
        for c in self.search(m.content, limit=30):
            if c.id == m.id or c.kind == "summary":
                continue
            t = topic_similarity(m.content, c.content)
            if t >= RELATED_AT and similarity(m.content, c.content) < DUPLICATE_AT:
                out.append((c, t))
        return sorted(out, key=lambda x: -x[1])[:limit]

    def flag_conflict(self, a: int, b: int, score: float, note: str = "") -> None:
        a, b = min(a, b), max(a, b)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO conflicts(a,b,score,note,created) VALUES (?,?,?,?,?)",
                            (a, b, round(score, 3), note[:300], time.time()))

    def open_conflicts(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM conflicts WHERE status='open' ORDER BY id").fetchall()
        out = []
        for r in rows:
            a, b = self.get(r["a"]), self.get(r["b"])
            if a and b:
                out.append({"id": r["id"], "score": r["score"], "note": r["note"], "a": a.model_dump(), "b": b.model_dump()})
        return out

    def conflicted_ids(self) -> set[int]:
        return {i for r in self.db.execute("SELECT a,b FROM conflicts WHERE status='open'") for i in r}

    def resolve_conflict(self, cid: int, action: Literal["keep_both", "keep_a", "keep_b"]) -> bool:
        r = self.db.execute("SELECT * FROM conflicts WHERE id=? AND status='open'", (cid,)).fetchone()
        if not r:
            return False
        if action == "keep_both":
            with self.db:
                self.db.execute("UPDATE conflicts SET status='kept_both' WHERE id=?", (cid,))
        else:
            self.forget(r["b"] if action == "keep_a" else r["a"])   # also removes this conflict row
        return True

    def search(self, text: str, limit: int = 12) -> list[Memory]:
        words = re.findall(r"\w{3,}", text.lower())
        if not words:
            return []
        q = " OR ".join(f'"{w}"' for w in words[:20])
        rows = self.db.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id=f.rowid "
            "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def relevant(self, text: str, small_db: int = 40, limit: int = 12) -> list[Memory]:
        """While memory is small include all of it. Otherwise use keyword search."""
        n = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return self.all() if n <= small_db else self.search(text, limit)

    # ---- conversation turns ----
    def add_turn(self, role: str, content: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO turns(role,content,ts) VALUES (?,?,?)", (role, content, time.time()))

    def recent_turns(self, n: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT role,content FROM turns ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def turns_window(self, base: int = 20, step: int = 16) -> list[dict]:
        """Recent conversation, with a start that moves forward `step` turns at a time instead of one. The beginning of
        the prompt then stays identical between replies, so the language model can reuse its earlier work (faster)."""
        n = self.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        k = n if n <= base else base + (n - base) % step
        return self.recent_turns(k)

    # ---- long-term conversation memory ----
    def get_meta(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def turns_to_summarize(self, window: int, chunk: int = 16) -> list[dict]:
        """The oldest not-yet-summarized turns that have slid out of the conversation window, `chunk` at a time
        (empty until a full chunk is out of view)."""
        done = int(self.get_meta("summarized_upto", "0"))
        oldest_visible = self.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1 OFFSET ?", (window - 1,)).fetchone()
        if not oldest_visible:
            return []
        rows = self.db.execute("SELECT id, role, content, ts FROM turns WHERE id > ? AND id < ? ORDER BY id LIMIT ?",
                               (done, oldest_visible[0], chunk)).fetchall()
        return [dict(r) for r in rows] if len(rows) == chunk else []

    def summaries(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories WHERE kind='summary' ORDER BY id")]

    def facts(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories WHERE kind!='summary' ORDER BY id")]

    def clear_turns(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM turns")

    # ---- export / backup / restore ----
    def export_json(self) -> str:
        return json.dumps([m.model_dump() for m in self.all()], indent=2)

    def import_json(self, data: str) -> int:
        """Returns how many were new (repeats of memories already kept are skipped)."""
        items = [Memory.model_validate(x) for x in json.loads(data)]
        return sum(not self.add(m.model_copy(update={"id": None}))[1] for m in items)

    def backup(self, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(dest)
        with out:
            self.db.backup(out)
        out.close()
        return dest

    @staticmethod
    def restore(backup: str | Path, target: str | Path) -> None:
        """Replace the target DB with a backup (call while the app is stopped)."""
        src = sqlite3.connect(backup)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()


# "remember that ..." / "remember: ..." typed by The owner → an explicit, confirmed memory.
REMEMBER_RE = re.compile(r"^\s*(?:echo[, ]+)?(?:please\s+)?remember(?:\s+that|:)\s+(.+)$", re.I | re.S)


def parse_remember(text: str) -> str | None:
    m = REMEMBER_RE.match(text)
    return m.group(1).strip().rstrip(".") + "." if m else None


def format_summaries(mems: list[Memory], title: str = "Earlier conversations (my own notes, may be incomplete):") -> str:
    return title + "\n" + "\n".join(f"- {m.content}" for m in mems) if mems else ""


PERIOD_WORDS = {"childhood": "childhood", "teen": "teenage years", "young_adult": "young adult years", "adult": "adult life",
                "recent": "recent"}


def life_details(m: Memory) -> str:
    """ " (childhood, summer 1974, with Mom, at the lake, told by loved one)" — only the parts that are known."""
    bits = [PERIOD_WORDS.get(m.period or "", ""), m.when_text or (str(m.year) if m.year else ""),
            f"with {m.people}" if m.people else "", f"at {m.place}" if m.place else "",
            f"told by {m.told_by.replace('_', ' ')}" if m.told_by and m.told_by.lower() != "owner" else ""]
    bits = [b for b in bits if b]
    return f" ({', '.join(bits)})" if bits else ""


def format_for_prompt(mems: list[Memory], conflicted: set[int] = frozenset()) -> str:
    if not mems:
        return "Things I remember: (nothing saved yet)"
    lines = [f"- {m.content}" + life_details(m) + ("" if m.status == "confirmed" else " (inferred, not confirmed)")
             + (" (may disagree with another memory; not sure which is right)" if m.id in conflicted else "")
             for m in mems]
    return "Things I remember (from the owner):\n" + "\n".join(lines)
