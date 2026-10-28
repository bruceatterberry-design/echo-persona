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

Kind = Literal["fact", "preference", "person", "project", "correction", "behavior", "summary"]
Status = Literal["confirmed", "inferred"]

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


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- durable memories ----
    def save(self, m: Memory) -> Memory:
        now = time.time()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO memories(kind,content,status,confidence,source,created,updated) VALUES (?,?,?,?,?,?,?)",
                (m.kind, m.content.strip(), m.status, m.confidence, m.source, now, now))
        return self.get(cur.lastrowid)

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
                "UPDATE memories SET kind=?,content=?,status=?,confidence=?,source=?,updated=? WHERE id=?",
                (new.kind, new.content.strip(), new.status, new.confidence, new.source, time.time(), mid))
        return self.get(mid)

    def correct(self, mid: int, content: str) -> Memory | None:
        """The owner corrected this memory: it becomes confirmed at full confidence."""
        return self.update(mid, content=content, status="confirmed", confidence=1.0, source="owner:correction")

    def forget(self, mid: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount > 0

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
        items = [Memory.model_validate(x) for x in json.loads(data)]
        for m in items:
            self.save(m.model_copy(update={"id": None}))
        return len(items)

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


def format_for_prompt(mems: list[Memory]) -> str:
    if not mems:
        return "Things I remember: (nothing saved yet)"
    lines = [f"- {m.content}" + ("" if m.status == "confirmed" else " (inferred, not confirmed)") for m in mems]
    return "Things I remember (from the owner):\n" + "\n".join(lines)
