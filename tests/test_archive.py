"""Phase 1 (docs/MEMORY_ARCHITECTURE.md): originals catalogued, finished days archived, turns grouped into episodes."""
import json
import lzma
import os
import time

import pytest

from memory import archive
from memory.store import Memory, MemoryStore

DAY = 86400


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.sqlite")


def add(store, ts, role, text, speaker=None):
    store.add_turn(role, text, speaker)
    store.db.execute("UPDATE turns SET ts=? WHERE id=(SELECT MAX(id) FROM turns)", (ts,))
    store.db.commit()


def test_episodes_split_on_quiet_and_clear_and_lead_both_ways(store):
    t0 = time.mktime((2026, 10, 30, 9, 0, 0, 0, 0, -1))
    for i in range(4):
        add(store, t0 + i * 60, "user" if i % 2 == 0 else "assistant", f"morning {i}", "loved_one" if i % 2 == 0 else None)
    for i in range(2):                                  # 2 hours later: a new conversation
        add(store, t0 + 7200 + i * 60, "user", f"later {i}", "owner")
    store.clear_turns()                                 # a clear ends a conversation too
    add(store, t0 + 7200 + 180, "user", "after clear")
    assert archive.build_episodes(store, now=t0 + 7200 + 200) == 2   # the last one is still going
    assert archive.build_episodes(store, now=t0 + 7200 + 200) == 0   # nothing twice
    assert archive.build_episodes(store, now=t0 + 9999) == 1
    eps = sorted(archive.episodes(store), key=lambda e: e["start"])
    assert [(e["first_turn"], e["last_turn"], e["turns"]) for e in eps] == [(1, 4, 4), (5, 6, 2), (7, 7, 1)]
    assert eps[0]["speakers"] == "loved_one" and eps[1]["speakers"] == "owner"
    for t in range(1, 8):                               # every turn leads to its episode, and the episode back to it
        e = archive.episode_for_turn(store, t)
        assert t in [x["id"] for x in archive.episode(store, e["id"])["turns_text"]]
    assert archive.episode(store, eps[0]["id"])["turns_text"][0]["content"] == "morning 0"


def test_time_range(store):
    t0 = time.mktime((2026, 10, 28, 9, 0, 0, 0, 0, -1))
    add(store, t0, "user", "monday"); add(store, t0 + DAY, "user", "tuesday")
    archive.build_episodes(store, now=t0 + 2 * DAY)
    assert [e["turns"] for e in archive.episodes(store, since=t0 + DAY - 60, until=t0 + DAY + 60)] == [1]


def test_notes_point_to_their_turns(store):
    t0 = time.time() - 5000
    for i in range(20):
        add(store, t0 + i, "user", f"t{i}")
    store.save(Memory(content="On Friday we talked about fishing.", kind="summary", status="inferred",
                      source="auto:conversation-summary", span="turns:1-16"))
    archive.build_episodes(store)
    e = archive.episode(store, archive.episode_for_turn(store, 3)["id"])
    assert [n["content"] for n in e["notes"]] == ["On Friday we talked about fishing."]


def test_old_notes_linked_only_when_provable(store):
    t0 = time.time() - 9000
    for i in range(48):
        add(store, t0 + i, "user", f"t{i}")
    for k in range(3):
        store.save(Memory(content=f"Note number {k} about subject {'abc'[k]} only.", kind="summary", status="inferred",
                          source="auto:conversation-summary"))
    store.set_meta("summarized_upto", "48")
    assert archive.link_old_notes(store) == 3
    assert [m.span for m in store.summaries()] == ["turns:1-16", "turns:17-32", "turns:33-48"]
    assert archive.link_old_notes(store) == 0                          # already done


def test_old_notes_not_linked_when_a_note_is_missing(store):
    for i in range(48):
        add(store, time.time() - 9000 + i, "user", f"t{i}")
    for k in range(2):                                                 # 3 chunks but only 2 notes: can't prove which
        store.save(Memory(content=f"Note number {k} about subject {'ab'[k]}.", kind="summary", status="inferred",
                          source="auto:conversation-summary"))
    store.set_meta("summarized_upto", "48")
    assert archive.link_old_notes(store) == 0 and all(m.span is None for m in store.summaries())


def test_finished_days_archived_once_read_only_and_checked(store, tmp_path):
    root = tmp_path / "data"; root.mkdir()
    t0 = time.mktime((2026, 10, 29, 20, 0, 0, 0, 0, -1))
    add(store, t0, "user", "Hey Echo, how was your day?", "loved_one"); add(store, t0 + 5, "assistant", "Good, thank you!")
    add(store, t0 + DAY, "user", "today, not finished yet")
    store.clear_turns()                                                # cleared turns are archived too
    assert archive.archive_days(store, root, now=t0 + DAY + 10) == ["raw/conversations/2026/2026-10-29.jsonl.xz"]
    assert archive.archive_days(store, root, now=t0 + DAY + 10) == []
    f = root / "raw/conversations/2026/2026-10-29.jsonl.xz"
    assert not os.access(f, os.W_OK)
    lines = archive.read_day(root, "2026-10-29")
    assert [l["content"] for l in lines] == ["Hey Echo, how was your day?", "Good, thank you!"] and lines[0]["speaker"] == "loved_one"
    digest = (f.parent / (f.name + ".sha256")).read_text().split()[0]
    assert digest == archive.sha256_file(f)
    assert archive.archive_days(store, root, now=t0 + 2 * DAY + 10) == ["raw/conversations/2026/2026-10-30.jsonl.xz"]


def test_dataset_drive_missing_is_fine(store, tmp_path):
    add(store, time.time() - 3 * DAY, "user", "hello")
    assert archive.archive_days(store, tmp_path / "not-connected") == []
    assert archive.catalogue(store, tmp_path / "not-connected") == {"connected": False}


def test_catalogue_adds_new_and_reads_each_file_once_then_verify_finds_damage(store, tmp_path):
    root = tmp_path / "data"
    (root / "raw/phone/s1").mkdir(parents=True); (root / "raw/voice").mkdir(parents=True)
    (root / "raw/phone/s1/take1.mp4").write_bytes(b"video" * 100)
    (root / "raw/voice/a.wav").write_bytes(b"audio")
    (root / "raw/voice/.DS_Store").write_bytes(b"x")
    c = archive.catalogue(store, root)
    assert c["added"] == 2 and c["changed"] == 0
    assert archive.catalogue(store, root)["added"] == 0 and archive.catalogue(store, root)["bytes_read"] == 0
    st = archive.status(store)
    assert st["originals"] == 2 and st["by_kind"]["phone"]["files"] == 1 and st["by_kind"]["voice"]["bytes"] == 5
    assert archive.verify(store, root)["ok"] == 2
    (root / "raw/voice/a.wav").write_bytes(b"AUDIO")                     # same size, damaged
    os.remove(root / "raw/phone/s1/take1.mp4")
    v = archive.verify(store, root)
    assert v["mismatched"] == ["raw/voice/a.wav"] and v["missing"] == ["raw/phone/s1/take1.mp4"]


def test_catalogue_respects_the_read_cap(store, tmp_path):
    root = tmp_path / "data"; (root / "raw/face").mkdir(parents=True)
    for i in range(3):
        (root / f"raw/face/{i}.jpg").write_bytes(b"x" * 1000)
    c = archive.catalogue(store, root, max_bytes=1500)
    assert c["added"] == 1 and c["more"]
    assert archive.catalogue(store, root)["added"] == 2


def test_turns_keep_who_spoke_and_api(tmp_path):
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    s = MemoryStore(tmp_path / "m.sqlite")
    add(s, time.time() - 5000, "user", "hello", "loved_one")
    archive.build_episodes(s)
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), s))
    e = c.get("/api/turns/1/episode").json()
    assert c.get(f"/api/episodes/{e['id']}").json()["turns_text"][0]["speaker"] == "loved_one"
    assert c.get("/api/episodes").json()[0]["id"] == e["id"] and c.get("/api/episodes/999").status_code == 404
    assert c.get("/api/archive/status").json()["episodes"] == 1
