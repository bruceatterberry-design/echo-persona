"""Durable local memory (SQLite). Nothing is saved unless the owner asks or approves.

Two layers:
  * memories: durable facts/preferences, each confirmed or inferred, with source + confidence.
  * turns:    the whole conversation log (the last few go to the LLM; older ones are kept as the original record).

Nothing is ever lost (Phase 0 of docs/MEMORY_ARCHITECTURE.md, the owner 2026-10-31):
  * every change to a memory is written to `versions` (before/after), so edits and corrections keep history;
  * "forget" hides a memory (Echo stops using it) instead of deleting it; it can be restored;
  * "clear conversation" only moves the start of Echo's view; the turns stay in the archive.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["fact", "preference", "person", "project", "correction", "behavior", "summary", "memory", "knowledge"]
Status = Literal["confirmed", "inferred", "learned"]   # learned = from a document or web page (memory/learn.py)
# "memory" = a life memory (an experience or event). Life period helps Echo tell childhood from recent life.
Period = Literal["childhood", "teen", "young_adult", "adult", "recent", "unknown"]
LIFE_FIELDS = ("period", "when_text", "year", "people", "place", "told_by")
# Columns added after the first version (v2: life memories; v3: hidden). Older databases get them on open, after a
# backup copy.
V2_COLUMNS = {"period": "TEXT", "when_text": "TEXT", "year": "INTEGER", "people": "TEXT", "place": "TEXT", "told_by": "TEXT"}
V3_COLUMNS = {"hidden": "REAL"}   # when it was forgotten (hidden); NULL = in use
V4_COLUMNS = {"span": "TEXT"}     # which part of the source it came from: "turns:17-32" for a conversation note
V4_TURN_COLUMNS = {"speaker": "TEXT",   # who was recognised by voice ("owner", "loved_one", "unknown"); NULL = typed/not known
                   "sent": "TEXT"}      # v7: the user message exactly as sent to the model (with my notes), see turns_window
SCHEMA_VERSION = "7"
HISTORY_FIELDS = ("kind", "content", "status", "confidence", "source", "span", *LIFE_FIELDS)

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
CREATE TABLE IF NOT EXISTS versions (   -- history of every change to a memory; never edited or deleted
  id INTEGER PRIMARY KEY,
  item_type TEXT NOT NULL,       -- "memory"
  item_id INTEGER NOT NULL,
  action TEXT NOT NULL,          -- create | edit | correct | merge | hide | restore
  before TEXT,                   -- JSON of the fields before (NULL for create)
  after TEXT,                    -- JSON of the fields after
  by TEXT NOT NULL,              -- who or what made the change: "owner:panel", "auto:conversation-summary", …
  at REAL NOT NULL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS versions_item ON versions(item_type, item_id);
-- L1 catalogue (memory/archive.py): every original on the dataset drive, by path, with its checksum
CREATE TABLE IF NOT EXISTS sources (
  path TEXT PRIMARY KEY,         -- relative to the dataset root: "raw/phone/…/take.mp4", "raw/conversations/2026/…"
  sha256 TEXT NOT NULL,
  kind TEXT NOT NULL,            -- conversation_day | phone | voice | face | speakers | other
  bytes INTEGER NOT NULL,
  mtime REAL NOT NULL,
  added REAL NOT NULL,
  verified REAL NOT NULL         -- last time the checksum was computed and matched
);
CREATE INDEX IF NOT EXISTS sources_sha ON sources(sha256);
-- Episodes (memory/archive.py): one conversation = turns with no gap longer than 20 minutes
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- conversation (recordings and events later)
  start REAL NOT NULL,
  end REAL NOT NULL,
  first_turn INTEGER NOT NULL,
  last_turn INTEGER NOT NULL,
  turns INTEGER NOT NULL,
  speakers TEXT,                 -- voices recognised in it, comma-separated
  summary TEXT,                  -- filled in later (Phase 3/4); notes are found through their spans meanwhile
  created REAL NOT NULL,
  UNIQUE(kind, first_turn)
);
CREATE INDEX IF NOT EXISTS episodes_time ON episodes(start);
-- Level 2 symbols (memory/entities.py): people, places, things, and facts about them, each pointing to its memory
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,            -- person | place | pet | thing | org | event | concept | self
  name TEXT NOT NULL,
  note TEXT,
  mentions INTEGER NOT NULL DEFAULT 0,
  first_seen REAL, last_seen REAL,
  created REAL NOT NULL,
  by TEXT NOT NULL               -- "seed" (config/entities.json) or "auto:extract"
);
CREATE TABLE IF NOT EXISTS aliases (entity_id INTEGER NOT NULL, alias TEXT NOT NULL COLLATE NOCASE,
                                    PRIMARY KEY(alias, entity_id));
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL,
  predicate TEXT NOT NULL,       -- from memory/entities.py PREDICATES
  object_id INTEGER,             -- another entity …
  value TEXT,                    -- … or plain words ("blue", "1974")
  status TEXT NOT NULL,          -- original | confirmed | inferred | learned
  confidence REAL NOT NULL,
  source TEXT NOT NULL,          -- "memory:12"
  span TEXT,                     -- the exact words that support it
  memory_id INTEGER,
  created REAL NOT NULL,
  retired REAL                   -- set when its memory was edited and read again (kept, never deleted)
);
CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject_id, predicate);
CREATE INDEX IF NOT EXISTS facts_object ON facts(object_id);
CREATE INDEX IF NOT EXISTS facts_memory ON facts(memory_id);
CREATE INDEX IF NOT EXISTS facts_subject_newest ON facts(subject_id, id);   -- a person's newest facts without sorting all
CREATE INDEX IF NOT EXISTS facts_object_newest ON facts(object_id, id);
CREATE INDEX IF NOT EXISTS facts_subject_kind ON facts(subject_id, predicate, memory_id);
CREATE INDEX IF NOT EXISTS facts_object_kind ON facts(object_id, predicate, memory_id);
CREATE TABLE IF NOT EXISTS relations (
  a_type TEXT NOT NULL, a_id INTEGER NOT NULL, rel TEXT NOT NULL, b_type TEXT NOT NULL, b_id INTEGER NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL, source TEXT, created REAL NOT NULL,
  PRIMARY KEY(a_type, a_id, rel, b_type, b_id)
);
CREATE INDEX IF NOT EXISTS relations_b ON relations(b_type, b_id);
CREATE INDEX IF NOT EXISTS relations_b_newest ON relations(b_type, b_id, rel, a_type, a_id);   -- newest mentions first
CREATE TABLE IF NOT EXISTS usage (     -- how often a memory was brought into a reply (memory/retrieve.py)
  item_type TEXT NOT NULL, item_id INTEGER NOT NULL, retrieved INTEGER NOT NULL DEFAULT 0, last REAL,
  PRIMARY KEY(item_type, item_id)
);
CREATE TABLE IF NOT EXISTS priority (  -- importance for ranking and the context pack (memory/consolidate.py)
  item_type TEXT NOT NULL, item_id INTEGER NOT NULL, score REAL NOT NULL, parts TEXT, updated REAL NOT NULL,
  PRIMARY KEY(item_type, item_id)
);
CREATE INDEX IF NOT EXISTS priority_score ON priority(item_type, score);
-- "has anything Echo always keeps in mind changed?" in one cheap read (memory/context.py), kept by the database itself
INSERT OR IGNORE INTO meta(key, value) VALUES ('changes', '0');
CREATE TRIGGER IF NOT EXISTS chg_mem_i AFTER INSERT ON memories BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_mem_u AFTER UPDATE ON memories BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_mem_d AFTER DELETE ON memories BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_fact_i AFTER INSERT ON facts BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_fact_u AFTER UPDATE ON facts BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_conf_i AFTER INSERT ON conflicts BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_conf_u AFTER UPDATE ON conflicts BEGIN UPDATE meta SET value=value+1 WHERE key='changes'; END;
-- meaning vectors changed (memory/vindex.py keeps them in RAM)
INSERT OR IGNORE INTO meta(key, value) VALUES ('vec_changes', '0');
CREATE TRIGGER IF NOT EXISTS chg_vec_i AFTER INSERT ON vectors BEGIN UPDATE meta SET value=value+1 WHERE key='vec_changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_vec_u AFTER UPDATE ON vectors BEGIN UPDATE meta SET value=value+1 WHERE key='vec_changes'; END;
CREATE TRIGGER IF NOT EXISTS chg_vec_d AFTER DELETE ON vectors BEGIN UPDATE meta SET value=value+1 WHERE key='vec_changes'; END;
CREATE TABLE IF NOT EXISTS extracted (   -- which memories were read for symbols, and which version of them
  memory_id INTEGER PRIMARY KEY, updated REAL NOT NULL, at REAL NOT NULL, model TEXT NOT NULL, facts INTEGER NOT NULL
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
    hidden: float | None = None                              # set when forgotten (hidden); read-only from outside
    span: str | None = Field(None, max_length=60)            # "turns:17-32": the part of the source it came from


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
        missing = {c: t for c, t in (V2_COLUMNS | V3_COLUMNS | V4_COLUMNS).items() if c not in have}
        have_t = {r[1] for r in self.db.execute("PRAGMA table_info(turns)")}
        missing_t = {c: t for c, t in V4_TURN_COLUMNS.items() if c not in have_t}
        if missing or missing_t:
            if self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]:   # real data: keep a copy first
                v = "v2" if "period" in missing else "v3" if "hidden" in missing else f"v{SCHEMA_VERSION}"
                self.backup(self.path.with_name(f"{self.path.stem}-before-{v}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"))
            with self.db:
                for c, t in missing.items():
                    self.db.execute(f"ALTER TABLE memories ADD COLUMN {c} {t}")
                for c, t in missing_t.items():
                    self.db.execute(f"ALTER TABLE turns ADD COLUMN {c} {t}")
        if self.get_meta("schema_version") != SCHEMA_VERSION:
            self.set_meta("schema_version", SCHEMA_VERSION)

    # ---- history (versions) ----
    def _log(self, mid: int, action: str, before: Memory | None, after: Memory | None, by: str, reason: str = "") -> None:
        """Write one history row. Call inside the same transaction as the change."""
        snap = lambda m: json.dumps({f: getattr(m, f) for f in HISTORY_FIELDS}) if m else None
        self.db.execute("INSERT INTO versions(item_type,item_id,action,before,after,by,at,reason) VALUES (?,?,?,?,?,?,?,?)",
                        ("memory", mid, action, snap(before), snap(after), by[:200], time.time(), reason[:300] or None))

    def history(self, mid: int) -> list[dict]:
        """Every change to one memory, oldest first."""
        rows = self.db.execute("SELECT * FROM versions WHERE item_type='memory' AND item_id=? ORDER BY id", (mid,))
        return [dict(r) | {"before": json.loads(r["before"]) if r["before"] else None,
                           "after": json.loads(r["after"]) if r["after"] else None} for r in rows]

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
            return (self.update(dup.id, _action="merge", _by=m.source, **upd) if upd else dup), True
        now = time.time()
        cols = ["kind", "content", "status", "confidence", "source", "span", "created", "updated", *LIFE_FIELDS]
        vals = [m.kind, m.content.strip(), m.status, m.confidence, m.source, m.span, now, now,
                *(getattr(m, f) for f in LIFE_FIELDS)]
        with self.db:
            cur = self.db.execute(f"INSERT INTO memories({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            new = self._row(cur.lastrowid)
            self._log(new.id, "create", None, new, m.source)
        return new, False

    def find_duplicate(self, content: str) -> Memory | None:
        best, score = None, 0.0
        for c in self.search(content, limit=20):
            s = similarity(content, c.content)
            if s > score:
                best, score = c, s
        return best if score >= DUPLICATE_AT else None

    def _row(self, mid: int) -> Memory | None:
        """A memory by id, hidden or not."""
        r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return Memory(**dict(r)) if r else None

    def get_many(self, ids) -> dict[int, Memory]:
        """Memories in use, by id, in one query."""
        ids = list(ids)
        out = {}
        for a in range(0, len(ids), 900):
            part = ids[a:a + 900]
            for r in self.db.execute(f"SELECT * FROM memories WHERE hidden IS NULL AND id IN ({','.join('?' * len(part))})", part):
                out[r["id"]] = Memory(**dict(r))
        return out

    def get(self, mid: int) -> Memory | None:
        """A memory in use (hidden ones are left out, like everywhere else Echo looks)."""
        m = self._row(mid)
        return m if m and m.hidden is None else None

    def all(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories WHERE hidden IS NULL ORDER BY id")]

    def update(self, mid: int, _action: str = "edit", _by: str = "owner:panel", **fields) -> Memory | None:
        cur = self.get(mid)
        if not cur:
            return None
        new = cur.model_copy(update={k: v for k, v in fields.items() if v is not None and k != "hidden"})
        Memory.model_validate(new.model_dump())  # re-validate
        with self.db:
            self.db.execute(
                "UPDATE memories SET kind=?,content=?,status=?,confidence=?,source=?,updated=?,"
                + ",".join(f"{f}=?" for f in LIFE_FIELDS) + " WHERE id=?",
                (new.kind, new.content.strip(), new.status, new.confidence, new.source, time.time(),
                 *(getattr(new, f) for f in LIFE_FIELDS), mid))
            self._log(mid, _action, cur, new, _by)
        return self.get(mid)

    def correct(self, mid: int, content: str, by: str = "owner:correction") -> Memory | None:
        """the owner corrected this memory: it becomes confirmed at full confidence. The old wording stays in history."""
        return self.update(mid, _action="correct", _by=by, content=content, status="confirmed", confidence=1.0,
                           source="owner:correction")

    def forget(self, mid: int, by: str = "owner:panel", reason: str = "") -> bool:
        """Hide a memory: Echo stops using it, but it is kept and can be restored (the owner, 2026-10-31: hide, not delete).
        Open contradiction flags that involve it are closed."""
        cur = self.get(mid)
        if not cur:
            return False
        with self.db:
            self.db.execute("UPDATE memories SET hidden=? WHERE id=? AND hidden IS NULL", (time.time(), mid))
            self.db.execute("UPDATE conflicts SET status='resolved' WHERE (a=? OR b=?) AND status='open'", (mid, mid))
            self._log(mid, "hide", cur, None, by, reason)
        return True

    def hidden(self) -> list[Memory]:
        """Forgotten (hidden) memories, most recently hidden first."""
        return [Memory(**dict(r)) for r in
                self.db.execute("SELECT * FROM memories WHERE hidden IS NOT NULL ORDER BY hidden DESC")]

    def restore_hidden(self, mid: int, by: str = "owner:panel") -> Memory | None:
        """Bring a forgotten memory back into use."""
        m = self._row(mid)
        if not m or m.hidden is None:
            return None
        with self.db:
            self.db.execute("UPDATE memories SET hidden=NULL WHERE id=?", (mid,))
            self._log(mid, "restore", None, m, by)
        return self.get(mid)

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
            "WHERE m.hidden IS NULL AND (v.memory_id IS NULL OR v.updated < m.updated OR v.model != ?) ORDER BY m.id LIMIT ?", (model, limit))
        return [Memory(**dict(r)) for r in rows]

    def set_vector(self, m: Memory, model: str, vec) -> None:
        import numpy as np
        v = np.asarray(vec, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        with self.db:
            self.db.execute("INSERT INTO vectors(memory_id,model,updated,vec) VALUES (?,?,?,?) ON CONFLICT(memory_id) "
                            "DO UPDATE SET model=excluded.model, updated=excluded.updated, vec=excluded.vec",
                            (m.id, model, m.updated, v.tobytes()))

    def similarities(self, qvec, model: str):
        """(memory ids, cosine similarities) for every memory in use, as numpy arrays. Brute force (see BENCHMARKS)."""
        import numpy as np
        rows = self.db.execute("SELECT v.memory_id, v.vec FROM vectors v JOIN memories m ON m.id=v.memory_id "
                               "WHERE v.model=? AND m.hidden IS NULL", (model,)).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        return np.array([r[0] for r in rows]), mat @ q

    def by_meaning(self, qvec, model: str, limit: int = 8, min_sim: float = 0.0) -> list[tuple[Memory, float]]:
        """Closest memories to a query vector (cosine), best first. Brute force: fine for tens of thousands."""
        import numpy as np
        rows = self.db.execute("SELECT v.memory_id, v.vec FROM vectors v JOIN memories m ON m.id=v.memory_id "
                               "WHERE v.model=? AND m.hidden IS NULL", (model,)).fetchall()
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
        return {i for r in self.db.execute("SELECT a,b FROM conflicts WHERE status='open'") for i in r
                if self.get(i)}

    def resolve_conflict(self, cid: int, action: Literal["keep_both", "keep_a", "keep_b"]) -> bool:
        r = self.db.execute("SELECT * FROM conflicts WHERE id=? AND status='open'", (cid,)).fetchone()
        if not r:
            return False
        if action == "keep_both":
            with self.db:
                self.db.execute("UPDATE conflicts SET status='kept_both' WHERE id=?", (cid,))
        else:
            self.forget(r["b"] if action == "keep_a" else r["a"], reason=f"conflict {cid}: {action}")   # also closes it
        return True

    def search(self, text: str, limit: int = 12) -> list[Memory]:
        words = re.findall(r"\w{3,}", text.lower())
        if not words:
            return []
        q = " OR ".join(f'"{w}"' for w in words[:20])
        rows = self.db.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id=f.rowid "
            "WHERE memories_fts MATCH ? AND m.hidden IS NULL ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def relevant(self, text: str, small_db: int = 40, limit: int = 12) -> list[Memory]:
        """While memory is small include all of it. Otherwise use keyword search."""
        n = self.db.execute("SELECT COUNT(*) FROM memories WHERE hidden IS NULL").fetchone()[0]
        return self.all() if n <= small_db else self.search(text, limit)

    def note_use(self, ids: list[int]) -> None:
        """These memories were brought into a reply (counts toward their priority)."""
        if ids:
            with self.db:
                self.db.executemany("INSERT INTO usage(item_type,item_id,retrieved,last) VALUES ('memory',?,1,?) "
                                    "ON CONFLICT(item_type,item_id) DO UPDATE SET retrieved=retrieved+1, last=excluded.last",
                                    [(i, time.time()) for i in ids])

    # ---- conversation turns ----
    def add_turn(self, role: str, content: str, speaker: str | None = None, sent: str | None = None) -> None:
        """`sent`: the message exactly as the model saw it (with memory notes), if different from what was said."""
        with self.db:
            self.db.execute("INSERT INTO turns(role,content,ts,speaker,sent) VALUES (?,?,?,?,?)",
                            (role, content, time.time(), speaker, sent if sent != content else None))

    def _view_from(self) -> int:
        """Turns up to this id were cleared from Echo's view (still kept in the archive)."""
        return int(self.get_meta("view_from", "0"))

    def recent_turns(self, n: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT role,content FROM turns WHERE id > ? ORDER BY id DESC LIMIT ?",
                               (self._view_from(), n)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def turns_window(self, keep: int = 8, most: int = 24, as_sent: bool = False, gap_s: float = 1800,
                     now: float | None = None) -> list[dict]:
        """Recent conversation for the model. The window grows until it holds `most` turns, or until someone comes back
        after `gap_s` of quiet (a new conversation), then starts again from the last `keep` turns. Between those resets
        every prompt begins exactly like the one before, so the model only reads what's new. Where the window starts is
        kept in meta (window_from, window_plain_until), so it doesn't move by accident.
        With as_sent, user turns are given as the model saw them (with the memory notes that came with them): models
        that can only resume from saved checkpoints (qwen3.5) otherwise re-read the whole conversation every turn.
        The turns carried over at a reset go without notes (everything is read again then anyway)."""
        start = max(int(self.get_meta("window_from", "0")), self._view_from() + 1)
        plain = int(self.get_meta("window_plain_until", "0"))
        rows = self.db.execute("SELECT id, role, content, sent, ts FROM turns WHERE id >= ? ORDER BY id", (start,)).fetchall()
        quiet = rows and (now or time.time()) - rows[-1]["ts"] > gap_s
        if len(rows) >= most or (quiet and len(rows) > keep):
            rows = rows[-keep:]
            plain = rows[-1]["id"]
            self.set_meta("window_from", str(rows[0]["id"]))
            self.set_meta("window_plain_until", str(plain))
        if not as_sent:
            return [{"role": r["role"], "content": r["content"]} for r in rows]
        return [{"role": r["role"], "content": r["content"] if r["id"] <= plain else (r["sent"] or r["content"])} for r in rows]

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
        done = max(done, self._view_from())   # cleared turns are archived, not summarized (as before)
        oldest_visible = self.db.execute("SELECT id FROM turns WHERE id > ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                                         (done, window - 1)).fetchone()
        if not oldest_visible:
            return []
        rows = self.db.execute("SELECT id, role, content, ts FROM turns WHERE id > ? AND id < ? ORDER BY id LIMIT ?",
                               (done, oldest_visible[0], chunk)).fetchall()
        return [dict(r) for r in rows] if len(rows) == chunk else []

    def summaries(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories WHERE kind='summary' AND hidden IS NULL ORDER BY id")]

    def facts(self) -> list[Memory]:
        return [Memory(**dict(r)) for r in self.db.execute("SELECT * FROM memories WHERE kind NOT IN ('summary','knowledge') AND hidden IS NULL ORDER BY id")]

    def clear_turns(self) -> int:
        """"Clear conversation": Echo's view starts fresh, but every turn stays in the archive (the owner, 2026-10-31).
        Returns how many turns are archived in all."""
        last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
        self.set_meta("view_from", str(last))
        clears = json.loads(self.get_meta("clears", "[]"))   # a clear also ends an episode (memory/archive.py)
        self.set_meta("clears", json.dumps(sorted(set(clears) | {last})))
        return last

    def archived_turns(self, since: float = 0.0, until: float | None = None) -> list[dict]:
        """The original conversation record (cleared turns included), oldest first."""
        rows = self.db.execute("SELECT id, role, content, ts, speaker FROM turns WHERE ts >= ? AND ts <= ? ORDER BY id",
                               (since, until if until is not None else time.time() + 1)).fetchall()
        return [dict(r) for r in rows]

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
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
        out.close()
        if ok != "ok":
            raise sqlite3.DatabaseError(f"backup failed its integrity check: {ok}")
        return dest

    def check(self) -> str:
        """SQLite's own integrity check of the live database: "ok" or the first problem found."""
        return self.db.execute("PRAGMA integrity_check").fetchone()[0]

    @staticmethod
    def restore(backup: str | Path, target: str | Path) -> None:
        """Replace the target DB with a backup (call while the app is stopped). The backup is checked first and the
        result after; a damaged backup is refused, so a good database is never replaced by a bad one."""
        src = sqlite3.connect(f"file:{Path(backup)}?mode=ro", uri=True)
        if (ok := src.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            src.close()
            raise sqlite3.DatabaseError(f"backup is damaged, not restored: {ok}")
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close()
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close()
        if ok != "ok":
            raise sqlite3.DatabaseError(f"restored database failed its integrity check: {ok}")


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
