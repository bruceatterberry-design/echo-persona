"""Memory snapshots, restore with replay, and restore drills (Phase 7 of docs/MEMORY_ARCHITECTURE.md, section 14).

  * snapshot():  a nightly copy of the memory database (SQLite online backup, integrity-checked, gzip-compressed, with
                 a .sha256 beside it). Kept: the newest 14 daily, one per month for 12 months, one per year forever.
                 Only files this module made (memory-daily-*, -monthly-*, -yearly-*) are ever pruned.
  * restore():   puts a snapshot back (refusing a damaged one), then replays every conversation turn that happened
                 after the snapshot from the read-only day archives on the dataset drive (memory/archive.py), so a
                 restore loses as little as possible. Vectors and the search index rebuild by themselves.
  * drill():     restores the newest snapshot onto a scratch copy (never the live database), checks it and times
                 every step; the result is shown on the Status page.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from memory import archive
from memory.store import MemoryStore

KEEP_DAILY, KEEP_MONTHLY = 14, 12
KINDS = ("daily", "monthly", "yearly")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def snapshot(store: MemoryStore, folder: Path, now: float | None = None) -> dict:
    """Today's snapshot (once a day), plus the month's and year's first one. Returns what was made and pruned."""
    now = now or time.time()
    folder.mkdir(parents=True, exist_ok=True)
    day, month, year = (time.strftime(f, time.localtime(now)) for f in ("%Y%m%d", "%Y%m", "%Y"))
    daily = folder / f"memory-daily-{day}.sqlite.gz"
    made, t0 = [], time.perf_counter()
    if not daily.exists():
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            raw = store.backup(Path(tmp) / "snap.sqlite")          # online backup + integrity_check (memory/store.py)
            with open(raw, "rb") as src, gzip.open(daily.with_suffix(".gz.part"), "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            raw_bytes = raw.stat().st_size
        os.replace(daily.with_suffix(".gz.part"), daily)
        daily.with_name(daily.name + ".sha256").write_text(f"{_sha(daily)}  {daily.name}\n")
        made.append(daily.name)
    else:
        raw_bytes = None
    for kind, stamp in (("monthly", month), ("yearly", year)):
        p = folder / f"memory-{kind}-{stamp}.sqlite.gz"
        if not p.exists():
            shutil.copy2(daily, p)
            p.with_name(p.name + ".sha256").write_text(f"{_sha(p)}  {p.name}\n")
            made.append(p.name)
    pruned = []
    for kind, keep in (("daily", KEEP_DAILY), ("monthly", KEEP_MONTHLY)):
        for old in sorted(folder.glob(f"memory-{kind}-*.sqlite.gz"))[:-keep]:
            old.unlink()
            old.with_name(old.name + ".sha256").unlink(missing_ok=True)
            pruned.append(old.name)
    return {"made": made, "pruned": pruned, "seconds": round(time.perf_counter() - t0, 2),
            "bytes": daily.stat().st_size, "raw_bytes": raw_bytes}


def snapshots(folder: Path) -> list[Path]:
    """All snapshots, newest first."""
    return sorted((p for k in KINDS for p in folder.glob(f"memory-{k}-*.sqlite.gz")),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def verify(p: Path) -> bool:
    side = p.with_name(p.name + ".sha256")
    return side.exists() and side.read_text().split()[0] == _sha(p)


def restore(snap: Path, target: Path, dataset_root: Path | None = None) -> dict:
    """Restore a snapshot to `target` (call with Echo stopped, or on a scratch path), then replay later turns from the
    day archives. A damaged snapshot is refused and the target is left untouched."""
    t0 = time.perf_counter()
    if not verify(snap):
        raise ValueError(f"{snap.name}: checksum doesn't match; not restored")
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        raw = Path(tmp) / "snap.sqlite"
        with gzip.open(snap, "rb") as src, open(raw, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        MemoryStore.restore(raw, target)                          # integrity-checked before and after
    t_restore = time.perf_counter() - t0
    replayed = 0
    if dataset_root and (dataset_root / archive.CONV_DIR).exists():
        db = sqlite3.connect(target)
        last = db.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
        for f in sorted((dataset_root / archive.CONV_DIR).glob("*/*.jsonl.xz")):
            rows = [t for t in archive.read_day(dataset_root, f.name[:10]) if t["id"] > last]
            with db:
                db.executemany("INSERT OR IGNORE INTO turns(id, role, content, ts, speaker) VALUES (?,?,?,?,?)",
                               [(t["id"], t["role"], t["content"], t["ts"], t.get("speaker")) for t in rows])
            replayed += len(rows)
        db.close()
    return {"restore_s": round(t_restore, 2), "replayed_turns": replayed, "seconds": round(time.perf_counter() - t0, 2)}


def counts(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("memories", "turns", "facts", "entities", "versions", "episodes", "sources")}
    db.close()
    return out


def drill(store: MemoryStore, folder: Path, dataset_root: Path | None = None) -> dict:
    """Snapshot now, restore it onto a scratch copy, check and time it. Never touches the live database."""
    t0 = time.perf_counter()
    live = counts(store.path)
    snap_info = snapshot(store, folder) if not any(folder.glob("memory-daily-*.sqlite.gz")) else None
    snap = snapshots(folder)[0]
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        target = Path(tmp) / "restored.sqlite"
        r = restore(snap, target, dataset_root)
        db = sqlite3.connect(target)
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        db.close()
        got = counts(target)
    # a snapshot can be older than the live database: memories may have grown since, but turns are replayed
    report = {"at": time.time(), "snapshot": snap.name, "snapshot_mb": round(snap.stat().st_size / 1e6, 2),
              "integrity": ok, "live": live, "restored": got, "replayed_turns": r["replayed_turns"],
              "restore_s": r["restore_s"], "total_s": round(time.perf_counter() - t0, 2),
              "turns_complete": got["turns"] >= live["turns"] - _today_turns(store),
              "ok": ok == "ok"}
    if snap_info:
        report["snapshot_made"] = snap_info
    (folder / "drill.json").write_text(json.dumps(report, indent=1))
    return report


def _today_turns(store: MemoryStore) -> int:
    """Turns from today aren't in a day archive yet (they are archived once the day is over)."""
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return store.db.execute("SELECT COUNT(*) FROM turns WHERE ts >= ?", (start,)).fetchone()[0]


if __name__ == "__main__":   # PYTHONPATH=. uv run python -m memory.backup [snapshot|drill]
    import sys
    from core.config import load_config
    cfg = load_config()
    s = MemoryStore(cfg.resolve(cfg.memory_db))
    folder = cfg.resolve(cfg.backup_dir)
    if sys.argv[1:] == ["snapshot"]:
        print(json.dumps(snapshot(s, folder), indent=1))
    else:
        print(json.dumps(drill(s, folder, cfg.resolve(cfg.dataset_root)), indent=1))
