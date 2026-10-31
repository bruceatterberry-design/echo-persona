"""Level 1, the originals (Phase 1 of docs/MEMORY_ARCHITECTURE.md).

Three jobs, all safe to run again and again (they only add, never change or delete):

  * archive_days(): every finished day of conversation is written once to the dataset drive as
    raw/conversations/YYYY/YYYY-MM-DD.jsonl.xz (one JSON line per turn), made read-only, with a .sha256 file beside it
    (checked by scripts/backup_dataset.sh). The turns also stay in the memory database.
  * catalogue(): every file under raw/ gets a row in `sources` (path, sha256, size). Only new or changed files are read.
  * build_episodes(): turns are grouped into conversations (a new one after 20 quiet minutes or a "Clear
    conversation"). Each episode knows its first and last turn, so any turn leads to its episode and back, and a
    conversation note (kind "summary", span "turns:a-b") leads to the turns it came from.

xz (Python's built-in lzma) is used rather than zstd, so nothing new has to be installed.
"""
from __future__ import annotations

import hashlib
import json
import lzma
import os
import re
import time
from datetime import datetime
from pathlib import Path

from memory.store import Memory, MemoryStore

GAP_S = 20 * 60           # quiet this long → the next turn starts a new episode
CONV_DIR = "raw/conversations"
KINDS = ("conversations", "phone", "voice", "face", "speakers")
SPAN_RE = re.compile(r"^turns:(\d+)-(\d+)$")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ---- conversation days → read-only files on the dataset drive ----
def day_lines(turns: list[dict]) -> bytes:
    return "".join(json.dumps({"id": t["id"], "role": t["role"], "content": t["content"], "ts": t["ts"],
                               "time": datetime.fromtimestamp(t["ts"]).isoformat(timespec="seconds"),
                               "speaker": t.get("speaker")}, ensure_ascii=False) + "\n" for t in turns).encode()


def archive_days(store: MemoryStore, root: Path, now: float | None = None) -> list[str]:
    """Write each finished day (before today) that isn't archived yet. Returns the paths written."""
    if not root.exists():
        return []   # dataset drive not connected: try again later
    today = _day(now or time.time())
    done = {r[0] for r in store.db.execute("SELECT path FROM sources WHERE kind='conversation_day'")}
    by_day: dict[str, list[dict]] = {}
    for t in store.archived_turns():
        if (d := _day(t["ts"])) < today:
            by_day.setdefault(d, []).append(t)
    written = []
    for d, turns in sorted(by_day.items()):
        rel = f"{CONV_DIR}/{d[:4]}/{d}.jsonl.xz"
        if rel in done:
            continue
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        data = day_lines(turns)
        if f.exists():   # written before but not recorded (e.g. a crash): keep it only if it holds exactly these turns
            if lzma.decompress(f.read_bytes()) != data:
                raise RuntimeError(f"{rel} already exists with different content; not overwritten")
        else:
            with open(f, "xb") as fh:   # never overwrite an original
                fh.write(lzma.compress(data, preset=9))
            f.chmod(0o444)
        digest = sha256_file(f)
        side = f.with_name(f.name + ".sha256")
        if not side.exists():
            side.write_text(f"{digest}  {f.name}\n")
            side.chmod(0o444)
        _record(store, rel, digest, "conversation_day", f)
        written.append(rel)
    return written


def read_day(root: Path, day: str) -> list[dict]:
    """The archived turns of one day, straight from the original file."""
    return [json.loads(x) for x in lzma.decompress((root / CONV_DIR / day[:4] / f"{day}.jsonl.xz").read_bytes()).splitlines()]


# ---- catalogue of every original ----
def _record(store: MemoryStore, rel: str, digest: str, kind: str, f: Path) -> None:
    st, now = f.stat(), time.time()
    with store.db:
        store.db.execute("INSERT INTO sources(path,sha256,kind,bytes,mtime,added,verified) VALUES (?,?,?,?,?,?,?) "
                         "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, bytes=excluded.bytes, "
                         "mtime=excluded.mtime, verified=excluded.verified",
                         (rel, digest, kind, st.st_size, st.st_mtime, now, now))


def _kind(rel: str) -> str:
    part = rel.split("/")[1] if rel.count("/") >= 1 else ""
    return "conversation_day" if part == "conversations" else part if part in KINDS else "other"


def catalogue(store: MemoryStore, root: Path, max_bytes: int | None = None) -> dict:
    """Add new or changed files under raw/ to `sources`. `max_bytes` caps how much is read in one run (the rest is
    picked up next time). Returns counts."""
    raw = root / "raw"
    if not raw.exists():
        return {"connected": False}
    known = {r[0]: (r[1], r[2]) for r in store.db.execute("SELECT path, bytes, mtime FROM sources")}
    added = changed = read = 0
    for dirpath, _dirs, files in os.walk(raw):
        for name in sorted(files):
            if name.startswith(".") or name.endswith(".sha256"):
                continue
            f = Path(dirpath) / name
            rel = f.relative_to(root).as_posix()
            st = f.stat()
            if known.get(rel) == (st.st_size, st.st_mtime):
                continue
            if max_bytes is not None and read + st.st_size > max_bytes and read:
                return {"connected": True, "added": added, "changed": changed, "bytes_read": read, "more": True}
            if rel in known:
                changed += 1   # an original that changed size or time: recorded, and reported (originals shouldn't)
            else:
                added += 1
            _record(store, rel, sha256_file(f), _kind(rel), f)
            read += st.st_size
    return {"connected": True, "added": added, "changed": changed, "bytes_read": read, "more": False}


def verify(store: MemoryStore, root: Path) -> dict:
    """Re-read every catalogued original and compare its checksum. Slow on big archives: run by hand or nightly."""
    ok, bad, missing = 0, [], []
    for rel, digest in store.db.execute("SELECT path, sha256 FROM sources ORDER BY path").fetchall():
        f = root / rel
        if not f.exists():
            missing.append(rel)
        elif sha256_file(f) == digest:
            ok += 1
            with store.db:
                store.db.execute("UPDATE sources SET verified=? WHERE path=?", (time.time(), rel))
        else:
            bad.append(rel)
    return {"ok": ok, "mismatched": bad, "missing": missing}


# ---- episodes ----
def build_episodes(store: MemoryStore, now: float | None = None) -> int:
    """Group turns not yet in an episode. The last group is only closed once it has been quiet for GAP_S.
    Returns how many episodes were made."""
    now = now or time.time()
    done = int(store.get_meta("episodes_upto", "0"))
    clears = set(json.loads(store.get_meta("clears", "[]")))
    rows = [dict(r) for r in store.db.execute(
        "SELECT id, role, ts, speaker FROM turns WHERE id > ? ORDER BY id", (done,))]
    groups, cur = [], []
    for t in rows:
        if cur and (t["ts"] - cur[-1]["ts"] > GAP_S or cur[-1]["id"] in clears):
            groups.append(cur)
            cur = []
        cur.append(t)
    if cur and now - cur[-1]["ts"] > GAP_S:
        groups.append(cur)
    for g in groups:
        who = sorted({t["speaker"] for t in g if t["speaker"]})
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO episodes(kind,start,end,first_turn,last_turn,turns,speakers,created) "
                             "VALUES ('conversation',?,?,?,?,?,?,?)",
                             (g[0]["ts"], g[-1]["ts"], g[0]["id"], g[-1]["id"], len(g), ",".join(who) or None, time.time()))
        store.set_meta("episodes_upto", str(g[-1]["id"]))
    return len(groups)


def episodes(store: MemoryStore, since: float = 0.0, until: float | None = None, limit: int = 100) -> list[dict]:
    """Episodes that overlap a time range, newest first."""
    rows = store.db.execute("SELECT * FROM episodes WHERE end >= ? AND start <= ? ORDER BY start DESC LIMIT ?",
                            (since, until if until is not None else time.time() + 1, limit))
    return [dict(r) for r in rows]


def episode(store: MemoryStore, eid: int) -> dict | None:
    """One episode with its original turns and the conversation notes made from them."""
    r = store.db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    if not r:
        return None
    e = dict(r)
    e["turns_text"] = [dict(t) for t in store.db.execute(
        "SELECT id, role, content, ts, speaker FROM turns WHERE id BETWEEN ? AND ? ORDER BY id", (e["first_turn"], e["last_turn"]))]
    e["notes"] = [m.model_dump() for m in notes_for_turns(store, e["first_turn"], e["last_turn"])]
    return e


def episode_for_turn(store: MemoryStore, turn_id: int) -> dict | None:
    r = store.db.execute("SELECT * FROM episodes WHERE ? BETWEEN first_turn AND last_turn", (turn_id,)).fetchone()
    return dict(r) if r else None


def span_of(m: Memory) -> tuple[int, int] | None:
    return (int(x.group(1)), int(x.group(2))) if m.span and (x := SPAN_RE.match(m.span)) else None


def notes_for_turns(store: MemoryStore, first: int, last: int) -> list[Memory]:
    """Conversation notes whose turns overlap first..last (one query; the span "turns:a-b" is read in SQL)."""
    rows = store.db.execute(
        "SELECT * FROM (SELECT *, CAST(substr(span, 7, instr(span, '-') - 7) AS INTEGER) AS a, "
        "CAST(substr(span, instr(span, '-') + 1) AS INTEGER) AS b FROM memories "
        "WHERE kind='summary' AND hidden IS NULL AND span LIKE 'turns:%') WHERE a <= ? AND b >= ? ORDER BY a", (last, first))
    return [Memory(**{k: r[k] for k in r.keys() if k not in ("a", "b")}) for r in rows]


# ---- one-time link of notes written before spans existed ----
def link_old_notes(store: MemoryStore, chunk: int = 16) -> int:
    """Notes from before 2026-10-31 don't say which turns they came from. They were written strictly in order, 16 turns
    each, starting at turn 1, never skipping an empty note *if* there is exactly one note per chunk up to
    `summarized_upto` and the turn ids have no gaps. Only then are spans filled in (exact, not guessed); otherwise
    nothing is changed. Returns how many were linked."""
    upto = int(store.get_meta("summarized_upto", "0"))
    rows = store.db.execute("SELECT id, span FROM memories WHERE kind='summary' AND source='auto:conversation-summary' "
                            "ORDER BY id").fetchall()   # hidden ones too: they were part of the sequence
    old = [r[0] for r in rows if not r[1]]
    newer = [(r[0], r[1]) for r in rows if r[1]]
    if not old or (newer and newer[0][0] < old[-1]):
        return 0
    end = len(old) * chunk                         # the old notes must cover exactly turns 1..end
    nxt = int(SPAN_RE.match(newer[0][1]).group(1)) if newer and SPAN_RE.match(newer[0][1]) else upto + 1
    first, n_turns = store.db.execute("SELECT MIN(id), COUNT(*) FROM turns WHERE id <= ?", (end,)).fetchone()
    if first != 1 or n_turns != end or nxt != end + 1:
        return 0   # the sequence can't be proven
    with store.db:
        for k, mid in enumerate(old):
            span = f"turns:{k * chunk + 1}-{(k + 1) * chunk}"
            store.db.execute("UPDATE memories SET span=? WHERE id=?", (span, mid))
            store.db.execute("INSERT INTO versions(item_type,item_id,action,before,after,by,at,reason) "
                             "VALUES ('memory',?,'edit',NULL,?,'auto:link-old-notes',?,?)",
                             (mid, json.dumps({"span": span}), time.time(), "span reconstructed from the note order"))
    return len(old)


def status(store: MemoryStore) -> dict:
    q = lambda sql: store.db.execute(sql).fetchone()
    n, b = q("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM sources")
    by = {k: {"files": c, "bytes": s} for k, c, s in store.db.execute(
        "SELECT kind, COUNT(*), SUM(bytes) FROM sources GROUP BY kind")}
    days, = q("SELECT COUNT(*) FROM sources WHERE kind='conversation_day'")
    eps, = q("SELECT COUNT(*) FROM episodes")
    turns, = q("SELECT COUNT(*) FROM turns")
    in_eps, = q("SELECT COALESCE(SUM(turns), 0) FROM episodes")
    return {"originals": n, "bytes": b, "by_kind": by, "conversation_days": days, "episodes": eps,
            "turns": turns, "turns_in_episodes": in_eps}


if __name__ == "__main__":   # PYTHONPATH=. uv run python -m memory.archive [verify]
    import sys
    from core.config import load_config
    cfg = load_config()
    s, root = MemoryStore(cfg.resolve(cfg.memory_db)), cfg.resolve(cfg.dataset_root)
    if sys.argv[1:] == ["verify"]:
        print(json.dumps(verify(s, root), indent=1))
    else:
        print("old notes linked:", link_old_notes(s))
        print("days archived:", archive_days(s, root))
        print("catalogue:", catalogue(s, root))
        print("episodes made:", build_episodes(s))
        print(json.dumps(status(s), indent=1))
