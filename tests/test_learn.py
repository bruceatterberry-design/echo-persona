"""Phase 7 (docs/MEMORY_ARCHITECTURE.md §9): learning from documents and web pages someone chose."""
import os
import subprocess

import httpx
import pytest

from memory.entities import Entities
from memory.learn import Learner, passages
from memory.retrieve import Retriever, format_for_message
from memory.store import Memory, MemoryStore
from tests.test_entities import SEEDS

TEXT = ("Kentucky Lake is a reservoir on the Tennessee River in Kentucky and Tennessee. It was made in 1944 when "
        "the Kentucky Dam was finished, and it is one of the largest man-made lakes in the eastern United States.\n\n"
        + "\n\n".join(f"Note {i}: crappie bite best in month {i} near {w} where the water is {d} feet deep and the "
                        f"bottom holds {b}, so anglers bring {t} and fish slowly along the edges in the morning."
                        for i, (w, d, b, t) in enumerate([("brush piles", 6, "stumps", "minnows"), ("the bridges", 12, "rock", "jigs"),
                                                          ("creek mouths", 4, "gravel", "spoons"), ("the dam", 20, "ledges", "spinners"),
                                                          ("docks", 8, "timber", "tubes"), ("points", 15, "sand", "crankbaits")])))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "dataset"; root.mkdir()
    s = MemoryStore(tmp_path / "m.sqlite")
    return s, Learner(s, root), root


def test_text_is_learned_as_marked_passages_and_the_original_kept(world):
    s, L, root = world
    jid = L.add_file("lake notes.txt", TEXT.encode(), title="Kentucky Lake")
    assert L.add_file("copy.txt", TEXT.encode()) == jid                      # same document twice: one job
    assert L.process_pending() == 1
    job = L.jobs()[0]
    assert job["status"] == "done" and job["passages"] == 2
    orig = root / job["original"]
    assert orig.read_bytes() == TEXT.encode() and not os.access(orig, os.W_OK)
    assert s.db.execute("SELECT kind FROM sources WHERE path=?", (job["original"],)).fetchone()[0] == "learned"
    k = [m for m in s.all() if m.kind == "knowledge"]
    assert all(m.status == "learned" and m.content.startswith("From “Kentucky Lake”:") and m.span.startswith("chars:")
               for m in k)


def test_learned_material_stays_out_of_life_facts_and_the_pack(world):
    s, L, root = world
    s.save(Memory(content="the owner went fishing at the lake."))
    L.add_file("lake.txt", TEXT.encode(), title="Kentucky Lake"); L.process_pending()
    e = Entities(s, SEEDS)
    assert all(m.kind != "knowledge" for m in e.pending(100))                # never read for facts about our lives
    assert all(m.kind != "knowledge" for m in s.facts())
    from memory.context import build
    text, ids = build(s, e, Retriever(s, e))
    assert "Kentucky Dam" not in text
    res = Retriever(s, e).retrieve("When was the Kentucky Dam finished?")
    note = format_for_message(res, s)
    assert "1944" in note and "learned from something I was given to read" in note


def test_forget_a_document_hides_all_its_passages_and_can_bring_them_back(world):
    s, L, root = world
    jid = L.add_file("lake.txt", TEXT.encode()); L.process_pending()
    n = len([m for m in s.all() if m.kind == "knowledge"])
    assert L.forget(jid) == n and not [m for m in s.all() if m.kind == "knowledge"]
    assert L.restore(jid) == n and L.jobs()[0]["status"] == "done"


@pytest.mark.parametrize("ext", [".pdf", ".docx", ".html"])
def test_other_formats(world, tmp_path, ext):
    s, L, root = world
    src = tmp_path / "doc.txt"; src.write_text(TEXT)
    if ext == ".pdf":
        out = tmp_path / "doc.pdf"; out.write_bytes(subprocess.run(["cupsfilter", str(src)], capture_output=True).stdout)
    elif ext == ".docx":
        out = tmp_path / "doc.docx"; subprocess.run(["textutil", "-convert", "docx", str(src), "-output", str(out)], check=True)
    else:
        out = tmp_path / "doc.html"
        out.write_text("<html><head><title>Lake</title><script>var x=1;</script></head><body><p>" + TEXT.replace("\n\n", "</p><p>") + "</p></body></html>")
    L.add_file(out.name, out.read_bytes()); L.process_pending()
    job = L.jobs()[0]
    assert job["status"] == "done", job["error"]
    text = " ".join(m.content for m in s.all() if m.kind == "knowledge")
    assert "Kentucky Dam" in text and "var x" not in text


def test_unreadable_and_unsupported_are_said_plainly(world):
    s, L, root = world
    with pytest.raises(ValueError):
        L.add_file("song.mp3", b"ID3....")
    L.add_file("broken.pdf", b"%PDF-1.4 not really"); L.process_pending()
    assert L.jobs()[0]["status"] == "failed" and L.jobs()[0]["error"]


def test_web_page(world, monkeypatch):
    s, L, root = world
    page = "<html><head><title>About Crappie</title></head><body><nav>menu</nav><p>" + TEXT + "</p></body></html>"
    from core import web
    monkeypatch.setattr(web, "_resolve", lambda host: ["93.184.215.14"])
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    jid = L.add_web("https://example.org/crappie")
    L.process_pending()
    job = L.jobs()[0]
    assert job["title"] == "About Crappie" and job["source"] == "https://example.org/crappie" and job["status"] == "done"
    assert "menu" not in " ".join(m.content for m in s.all() if m.kind == "knowledge")
    with pytest.raises(ValueError):
        L.add_web("file:///etc/passwd")


def test_passages_cut_at_paragraphs():
    ps = passages(TEXT, 300)
    assert all(len(p[2]) <= 300 for p in ps) and ps[0][0] == 0 and all(p[2][0].isupper() for p in ps)


def test_drive_missing(tmp_path):
    s = MemoryStore(tmp_path / "m.sqlite")
    with pytest.raises(RuntimeError):
        Learner(s, tmp_path / "not-connected").add_file("a.txt", b"hello there")
