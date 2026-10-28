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
    monkeypatch.setattr(sm, "summarize", lambda host, model, turns, helper=None: f"On Monday we talked about the lake ({len(turns)} lines).")
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
