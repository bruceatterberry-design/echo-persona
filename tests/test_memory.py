import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import Memory, MemoryStore, format_for_prompt, parse_remember


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    yield s
    s.close()


def test_save_retrieve_update_forget(store):
    m = store.save(Memory(content="My favorite food is barbecue ribs.", kind="preference"))
    assert store.get(m.id).content.startswith("My favorite")
    store.update(m.id, kind="fact")
    assert store.get(m.id).kind == "fact"
    assert store.forget(m.id) and store.get(m.id) is None
    assert not store.forget(m.id)


def test_correction_marks_confirmed(store):
    m = store.save(Memory(content="The owner likes tea.", status="inferred", confidence=0.4))
    c = store.correct(m.id, "The owner prefers coffee, not tea.")
    assert (c.status, c.confidence, c.source) == ("confirmed", 1.0, "owner:correction")
    assert store.search("coffee")[0].id == m.id
    assert store.search("tea")[0].id == m.id  # FTS index follows updates
    store.correct(m.id, "The owner prefers coffee.")
    assert store.search("tea") == []


def test_validation_rejects_bad_memory():
    with pytest.raises(Exception):
        Memory(content="")
    with pytest.raises(Exception):
        Memory(content="x", status="guessed")


def test_survives_restart(tmp_path):
    p = tmp_path / "m.sqlite"
    s1 = MemoryStore(p)
    s1.save(Memory(content="My daughter's name is Example."))
    s1.add_turn("user", "hello")
    s1.close()
    s2 = MemoryStore(p)
    assert [m.content for m in s2.all()] == ["My daughter's name is Example."]
    assert s2.recent_turns() == [{"role": "user", "content": "hello"}]


def test_export_backup_restore(tmp_path, store):
    store.save(Memory(content="I grew up near the river."))
    exported = store.export_json()
    b = store.backup(tmp_path / "bk" / "b.sqlite")
    store.forget(store.all()[0].id)
    MemoryStore.restore(b, tmp_path / "restored.sqlite")
    assert MemoryStore(tmp_path / "restored.sqlite").all()[0].content == "I grew up near the river."
    fresh = MemoryStore(tmp_path / "fresh.sqlite")
    assert fresh.import_json(exported) == 1


def test_relevant_uses_search_when_large(store):
    for i in range(45):
        store.save(Memory(content=f"filler memory number {i}"))
    store.save(Memory(content="The owner loves fishing on Saturdays."))
    got = store.relevant("do I like fishing?")
    assert any("fishing" in m.content for m in got) and len(got) <= 12


def test_parse_remember():
    assert parse_remember("Remember that my dog is named Max") == "my dog is named Max."
    assert parse_remember("echo, remember: I was born in Ohio.") == "I was born in Ohio."
    assert parse_remember("Do you remember my dog?") is None


def test_prompt_marks_inferred():
    out = format_for_prompt([Memory(content="Likes jazz.", status="inferred")])
    assert "inferred" in out
    assert "nothing saved" in format_for_prompt([])


class CapturingBackend(EchoBackend):
    def __init__(self):
        self.seen = []

    async def chat(self, messages):
        self.seen.append(messages)
        return await super().chat(messages)


def test_chat_remember_and_recall_across_restart(tmp_path):
    p = tmp_path / "m.sqlite"
    with TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(p))).websocket_connect("/ws") as ws:
        ws.receive_json()  # history
        ws.send_json({"text": "Remember that my favorite color is blue"})
        saved = ws.receive_json()
        assert saved["type"] == "memory_saved" and "blue" in saved["memory"]["content"]
        for _ in range(3):
            ws.receive_json()
    # "restart": new app, new store object, same file
    b = CapturingBackend()
    with TestClient(create_app(PersonaConfig(), b, MemoryStore(p))).websocket_connect("/ws") as ws:
        hist = ws.receive_json()
        assert hist["turns"][0]["content"].startswith("Remember that")
        ws.send_json({"text": "What is my favorite color?"})
        for _ in range(3):
            ws.receive_json()
    assert "favorite color is blue" in b.seen[0][0]["content"]


def test_memory_api(tmp_path):
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    mid = c.post("/api/memory", json={"content": "I served in the Navy."}).json()["id"]
    assert c.patch(f"/api/memory/{mid}", json={"content": "I served in the Army."}).json()["status"] == "confirmed"
    assert "Army" in c.get("/api/memory/export").text
    assert c.delete(f"/api/memory/{mid}").status_code == 200
    assert c.delete(f"/api/memory/{mid}").status_code == 404
    assert c.get("/api/memory").json() == []


def test_turn_window_moves_in_steps(store):
    for i in range(35):
        store.add_turn("user", f"m{i}")
    starts = []
    for i in range(35, 47):
        store.add_turn("user", f"m{i}")
        starts.append(store.turns_window()[0]["content"])
    assert len(set(starts)) <= 2          # the start only moves every 16 turns
    assert store.turns_window()[-1]["content"] == "m46" and 20 <= len(store.turns_window()) < 36


def test_only_out_of_view_turns_are_summarized_in_chunks(store):
    for i in range(30):
        store.add_turn("user" if i % 2 == 0 else "assistant", f"t{i}")
    assert store.turns_to_summarize(window=20, chunk=16) == []        # only 10 out of view: wait for a full chunk
    for i in range(30, 40):
        store.add_turn("user", f"t{i}")
    chunk = store.turns_to_summarize(window=20, chunk=16)
    assert [t["content"] for t in chunk] == [f"t{i}" for i in range(16)]
    store.set_meta("summarized_upto", str(chunk[-1]["id"]))
    assert store.turns_to_summarize(window=20, chunk=16) == []        # next chunk not fully out of view yet


def test_summarize_pending_saves_inferred_notes(store, monkeypatch):
    import memory.summarize as sm
    for i in range(60):
        store.add_turn("user", f"we talked about the lake {i}")
    topics = iter(["the lake and the fishing trip", "Grandma's garden, the old barn and supper"])   # distinct notes (repeats merge)
    monkeypatch.setattr(sm, "summarize", lambda host, model, turns, helper=None: f"On Monday we talked about {next(topics)} ({len(turns)} lines).")
    assert sm.summarize_pending(store, window=20, host="x", model="y") == 2
    notes = store.summaries()
    assert len(notes) == 2 and all(n.status == "inferred" and n.source == "auto:conversation-summary" for n in notes)
    assert store.facts() == [] and sm.summarize_pending(store, window=20, host="x", model="y") == 0


def test_prompt_has_latest_notes_and_finds_older_ones(tmp_path):
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    from fastapi.testclient import TestClient

    class Capture(EchoBackend):
        def __init__(self): self.seen = []
        async def chat(self, messages): self.seen.append(messages); return await super().chat(messages)

    s = MemoryStore(tmp_path / "m.sqlite")
    s.save(Memory(content="On Monday we talked about the trip to the lake and the old cabin.", kind="summary", status="inferred"))
    for d in ("Tuesday", "Wednesday", "Thursday"):
        s.save(Memory(content=f"On {d} we talked about dinner.", kind="summary", status="inferred"))
    s.save(Memory(content="My favorite color is blue."))
    b = Capture()
    with TestClient(create_app(PersonaConfig(), b, s)).websocket_connect("/ws") as ws:
        ws.receive_json(); ws.send_json({"text": "Do you remember the cabin at the lake?"})
        while ws.receive_json()["type"] != "reply":
            pass
    system, user = b.seen[0][0]["content"], b.seen[0][-1]["content"]
    assert "Thursday" in system and "Monday" not in system and "favorite color is blue" in system
    assert "old cabin" in user and user.endswith("Do you remember the cabin at the lake?")
    assert s.recent_turns()[0]["content"] == "Do you remember the cabin at the lake?"   # history keeps only what was said


def test_generic_questions_pull_in_no_old_notes(tmp_path):
    from core.server import content_words
    note = "On Monday we talked about the trip to the lake and the old cabin."
    assert len(content_words("How was your day?") & content_words(note)) < 2
    assert len(content_words("Do you remember the cabin at the lake?") & content_words(note)) >= 2


# ---- life memories + duplicates (Phase 2a, 2026-10-30) ----
def test_life_memory_fields_persist_and_show_in_prompt(tmp_path):
    p = tmp_path / "m.sqlite"
    s1 = MemoryStore(p)
    s1.save(Memory(kind="memory", content="We caught a huge catfish at the lake.", period="childhood",
                   when_text="summer 1974", year=1974, people="Dad", place="the lake", told_by="owner"))
    s1.close()
    m = MemoryStore(p).all()[0]
    assert (m.kind, m.period, m.year, m.people) == ("memory", "childhood", 1974, "Dad")
    line = format_for_prompt([m])
    assert "childhood, summer 1974, with Dad, at the lake" in line and "told by" not in line   # the owner is the default teller
    assert "told by loved one" in format_for_prompt([m.model_copy(update={"told_by": "loved_one"})])


def test_bad_period_rejected():
    with pytest.raises(Exception):
        Memory(content="x", period="medieval")


def test_near_repeats_do_not_multiply(store):
    a, dup1 = store.add(Memory(content="My favorite food is barbecue ribs."))
    b, dup2 = store.add(Memory(content="my favorite food is BBQ... no: barbecue ribs!".replace("BBQ... no: ", "")))
    assert not dup1 and dup2 and b.id == a.id and len(store.all()) == 1
    store.add(Memory(content="the owner prefers tea."))
    store.add(Memory(content="the owner prefers coffee, not tea."))   # different claim: kept separately
    assert len(store.all()) == 3


def test_repeat_upgrades_inferred_to_confirmed_never_down(store):
    m = store.save(Memory(content="I grew up near the river.", status="inferred", confidence=0.4, source="auto:x"))
    up, dup = store.add(Memory(content="I grew up near the river", status="confirmed", source="owner:chat", period="childhood"))
    assert dup and up.id == m.id and (up.status, up.source, up.period) == ("confirmed", "owner:chat", "childhood")
    same, _ = store.add(Memory(content="I grew up near the river.", status="inferred", confidence=0.2))
    assert same.status == "confirmed" and same.confidence == 1.0


def test_import_skips_repeats(store):
    store.save(Memory(content="I grew up near the river."))
    assert store.import_json(store.export_json()) == 0 and len(store.all()) == 1


def test_old_database_migrates_with_backup(tmp_path):
    import sqlite3
    p = tmp_path / "old.sqlite"
    db = sqlite3.connect(p)
    from memory.store import SCHEMA   # the v1 schema (new columns are only added by the migration)
    db.executescript(SCHEMA + "INSERT INTO memories VALUES (1,'fact','Old fact.','confirmed',1.0,'owner:chat',1,1);")
    db.commit(); db.close()
    s = MemoryStore(p)
    assert s.all()[0].content == "Old fact." and s.all()[0].period is None
    assert list(tmp_path.glob("old-before-v2-*.sqlite"))
    s.update(1, period="adult")
    assert s.get(1).period == "adult"


def test_memory_api_duplicate_flag_and_life_patch(tmp_path):
    cfg = PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), backup_dir=str(tmp_path / "bk"))
    c = TestClient(create_app(cfg, backend=EchoBackend()))
    r1 = c.post("/api/memory", json={"content": "I learned to swim at Grandma's pond."}).json()
    r2 = c.post("/api/memory", json={"content": "I learned to swim at grandma's pond"}).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True and r2["id"] == r1["id"]
    r = c.patch(f"/api/memory/{r1['id']}", json={"kind": "memory", "period": "childhood", "place": "Grandma's pond"}).json()
    assert (r["kind"], r["period"], r["place"]) == ("memory", "childhood", "Grandma's pond")


# ---- possible contradictions (Phase 2b, 2026-10-30) ----
from memory import conflicts as cc


def test_related_finds_same_topic_not_repeats_or_notes(store):
    tea = store.save(Memory(content="the owner prefers tea in the morning."))
    store.save(Memory(content="On Monday we talked about tea.", kind="summary", status="inferred"))
    store.save(Memory(content="My truck is a blue Ford."))
    new = store.save(Memory(content="the owner prefers coffee in the morning, not tea."))
    assert [m.id for m, _ in store.related(new)] == [tea.id]


def test_judge_decides_and_nothing_is_overwritten(store):
    old = store.save(Memory(content="My dog is named Max."))
    new = store.save(Memory(content="My dog is named Rex."))
    assert cc.check(store, new, lambda a, b: (True, "different names")) == 1
    [c] = store.open_conflicts()
    assert (c["a"]["id"], c["b"]["id"], c["note"]) == (old.id, new.id, "different names")
    assert len(store.all()) == 2                                 # both still kept
    assert "may disagree" in format_for_prompt(store.all(), store.conflicted_ids())


def test_judge_says_compatible_no_flag(store):
    store.save(Memory(content="I grew up in Ohio near a river."))
    new = store.save(Memory(content="I grew up in Ohio on a farm."))
    assert cc.check(store, new, lambda a, b: (False, "both can be true")) == 0 and store.open_conflicts() == []


def test_no_judge_flags_only_close_pairs(store):
    store.save(Memory(content="My dog is named Max."))
    close = store.save(Memory(content="My dog is named Rex."))
    assert cc.check(store, close, None) == 1
    store.save(Memory(content="I grew up in Ohio near a big river and woods."))
    loose = store.save(Memory(content="I grew up in Ohio and later moved to Texas for work."))
    assert cc.check(store, loose, lambda a, b: None) == 0        # judge failed + loosely related → not flagged


def test_resolve_keep_both_keep_one_and_forget_cleans_up(store):
    a = store.save(Memory(content="My dog is named Max."))
    b = store.save(Memory(content="My dog is named Rex."))
    store.flag_conflict(a.id, b.id, 0.6)
    store.flag_conflict(a.id, b.id, 0.6)                         # same pair only once
    [c] = store.open_conflicts()
    assert store.resolve_conflict(c["id"], "keep_both") and store.open_conflicts() == []
    assert len(store.all()) == 2 and not store.resolve_conflict(c["id"], "keep_a")
    d = store.save(Memory(content="My dog is named Buddy."))
    store.flag_conflict(a.id, d.id, 0.6)
    [c2] = store.open_conflicts()
    assert store.resolve_conflict(c2["id"], "keep_b") and store.get(a.id) is None and store.get(d.id)
    store.flag_conflict(b.id, d.id, 0.6)
    store.forget(d.id)
    assert store.open_conflicts() == []


def test_conflicts_survive_restart(tmp_path):
    p = tmp_path / "m.sqlite"
    s1 = MemoryStore(p)
    a, b = s1.save(Memory(content="My dog is Max.")), s1.save(Memory(content="My dog is Rex."))
    s1.flag_conflict(a.id, b.id, 0.5, "names differ")
    s1.close()
    assert MemoryStore(p).open_conflicts()[0]["note"] == "names differ"


def test_conflict_api_flow(tmp_path):
    import time as _t
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"),
                              conflict_judge=lambda a, b: (True, "names differ")))
    c.post("/api/memory", json={"content": "My dog is named Max."})
    c.post("/api/memory", json={"content": "My dog is named Rex."})
    for _ in range(50):                                          # checked in the background
        if (got := c.get("/api/memory/conflicts").json()):
            break
        _t.sleep(0.05)
    assert got and got[0]["note"] == "names differ"
    assert c.post(f"/api/memory/conflicts/{got[0]['id']}", json={"action": "keep_b"}).status_code == 200
    assert [m["content"] for m in c.get("/api/memory").json()] == ["My dog is named Rex."]
    assert c.post(f"/api/memory/conflicts/{got[0]['id']}", json={"action": "keep_b"}).status_code == 404
    assert c.post("/api/memory/conflicts/1", json={"action": "delete_all"}).status_code == 422


# ---- search by meaning (Phase 2c, 2026-10-30) ----
from memory.embed import Embedder

SYN = {"pets": "cat", "kitties": "cat", "cats": "cat", "dinner": "food", "supper": "food", "pie": "food"}


class FakeEmbedder(Embedder):
    """Bag of words with a few synonyms, hashed into 64 numbers: 'pets' lands near 'cats' without sharing a word."""
    def __init__(self):
        super().__init__("http://127.0.0.1:9", model="fake-embed")

    def _embed(self, texts, timeout=None):
        import numpy as np, re, zlib
        out = []
        for t in texts:
            v = np.zeros(64, np.float32) + 0.01
            for w in re.findall(r"[a-z]+", t.lower().split(":", 1)[-1]):
                if len(w) >= 3 and w not in ("what", "the", "have", "should", "for", "you", "any", "and", "her"):
                    v[zlib.crc32(SYN.get(w, w).encode()) % 64] += 1
            out.append(v.tolist())
        return out


def test_vectors_made_once_remade_after_edit_and_removed_on_forget(store):
    e = FakeEmbedder()
    a = store.save(Memory(content="The owner has two dogs, Max and Ruby."))
    store.save(Memory(content="Sam loves her mom's apple pie."))
    assert e.index(store) == 2 and e.index(store) == 0
    import time as _t; _t.sleep(0.01)
    store.correct(a.id, "The owner has two dogs, Max and Ruby, and a cat.")
    assert [m.id for m in store.needs_vectors(e.model)] == [a.id] and e.index(store) == 1
    assert store.needs_vectors("another-model")                         # model changed → all remade
    store.forget(a.id)
    assert store.db.execute("SELECT COUNT(*) FROM vectors WHERE memory_id=?", (a.id,)).fetchone()[0] == 0


def test_by_meaning_finds_without_shared_words(store):
    e = FakeEmbedder()
    cats = store.save(Memory(content="The owner has two dogs, Max and Ruby."))
    pie = store.save(Memory(content="Sam loves her mom's apple pie."))
    e.index(store)
    assert store.search("Do you have any pets?") == []                 # keywords miss it
    assert store.by_meaning(e.query("Do you have any pets?"), e.model, 1)[0][0].id == cats.id
    assert store.by_meaning(e.query("What's for supper?"), e.model, 1)[0][0].id == pie.id
    assert store.by_meaning(e.query("pets"), e.model, 5, min_sim=0.99) == []


def test_embedder_down_returns_none_and_counts():
    e = Embedder("http://127.0.0.1:9")
    assert e.query("hello") is None and e.failures == 1
    assert e.index(MemoryStore(":memory:")) == 0


def test_prompt_gets_notes_found_by_meaning(tmp_path):
    import core.server as srv
    store = MemoryStore(tmp_path / "m.sqlite")
    note = store.save(Memory(content="On Tuesday Sam told me about her mom's apple pie.", kind="summary", status="inferred"))
    for i in range(4):   # newer notes: these are in the prompt anyway
        store.save(Memory(content=f"On Wednesday we talked about item {i} {'x' * i}.", kind="summary", status="inferred"))
    e = FakeEmbedder(); e.index(store)
    b = CapturingBackend()

    class Stream(type(b)):
        async def chat_stream(self, messages):
            self.seen.append(messages)
            async for p in EchoBackend.chat_stream(self, messages):
                yield p
    b.__class__ = Stream
    old = srv.MEANING_MIN; srv.MEANING_MIN = 0.3
    try:
        with TestClient(create_app(PersonaConfig(memory_db=str(tmp_path / "m.sqlite")), b, store, embedder=e)).websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "What should we have for dinner?"})
            while ws.receive_json().get("state") != "idle":
                pass
    finally:
        srv.MEANING_MIN = old
    assert "apple pie" in b.seen[0][-1]["content"] and "apple pie" not in b.seen[0][0]["content"]
