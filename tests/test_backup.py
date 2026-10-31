"""Phase 7 (docs/MEMORY_ARCHITECTURE.md §14): snapshots kept on a schedule, restore with replay, drills."""
import gzip
import time
from datetime import datetime

import pytest

from memory import archive, backup
from memory.store import Memory, MemoryStore

DAY = 86400


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    s.save(Memory(content="Sam is a nurse."))
    return s


def test_snapshots_are_kept_on_a_schedule(store, tmp_path):
    folder = tmp_path / "backups"
    (folder).mkdir()
    (folder / "memory-before-phase1-x.sqlite").write_text("mine, never touched")
    t0 = datetime(2026, 1, 1, 3).timestamp()
    for d in range(70):                                              # 70 nights
        backup.snapshot(store, folder, now=t0 + d * DAY)
    names = sorted(p.name for p in folder.iterdir())
    assert len([n for n in names if n.startswith("memory-daily") and n.endswith(".gz")]) == 14
    assert [n for n in names if n.startswith("memory-monthly") and n.endswith(".gz")] == [
        "memory-monthly-202601.sqlite.gz", "memory-monthly-202602.sqlite.gz", "memory-monthly-202603.sqlite.gz"]
    assert "memory-yearly-2026.sqlite.gz" in names and "memory-before-phase1-x.sqlite" in names
    assert backup.snapshot(store, folder, now=t0 + 69 * DAY)["made"] == []          # once a day
    snap = backup.snapshots(folder)[0]
    assert backup.verify(snap) and gzip.open(snap).read(16).startswith(b"SQLite format 3")


def test_restore_replays_turns_said_after_the_snapshot(store, tmp_path):
    root, folder = tmp_path / "data", tmp_path / "backups"
    root.mkdir()
    t0 = time.time() - 5 * DAY
    for i in range(4):
        store.add_turn("user", f"before {i}")
    store.db.execute("UPDATE turns SET ts=?", (t0,)); store.db.commit()
    backup.snapshot(store, folder)
    for i in range(3):                                              # said after the snapshot, on an earlier day
        store.add_turn("user", f"after {i}")
    store.db.execute("UPDATE turns SET ts=? WHERE content LIKE 'after%'", (t0 + DAY,)); store.db.commit()
    archive.archive_days(store, root)
    target = tmp_path / "restored.sqlite"
    r = backup.restore(backup.snapshots(folder)[0], target, root)
    assert r["replayed_turns"] == 3
    got = [t["content"] for t in MemoryStore(target).archived_turns()]
    assert got == [f"before {i}" for i in range(4)] + [f"after {i}" for i in range(3)]


def test_damaged_snapshot_is_refused(store, tmp_path):
    folder = tmp_path / "backups"
    backup.snapshot(store, folder)
    snap = backup.snapshots(folder)[0]
    data = bytearray(snap.read_bytes()); data[100] ^= 0xFF; snap.write_bytes(bytes(data))
    target = tmp_path / "live.sqlite"
    target.write_text("the good database")
    with pytest.raises(ValueError):
        backup.restore(snap, target)
    assert target.read_text() == "the good database"


def test_drill_never_touches_the_live_database(store, tmp_path):
    before = store.path.read_bytes()
    r = backup.drill(store, tmp_path / "backups")
    assert r["ok"] and r["integrity"] == "ok" and r["restored"]["memories"] == 1
    assert store.path.read_bytes() == before and (tmp_path / "backups" / "drill.json").exists()
