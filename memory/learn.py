"""Learning from documents and web pages the owner or the loved one choose (Phase 7 of docs/MEMORY_ARCHITECTURE.md, section 9).

acquire → keep the original → read → split → save as knowledge → (meaning vectors, search) — each a step of a job in
the `learn_jobs` table, so it is resumable and visible ("Learn" on the My memories page, GET /api/learn).

  * Only what a person asks for: an uploaded file or a web address. Echo never goes looking on his own.
  * The original is kept read-only on the dataset drive (raw/learned/YYYY/<sha256>.<ext>) with its checksum in the
    catalogue (sources, kind "learned"); a web page is kept as it was fetched, with the address and date.
  * The text becomes memories of kind "knowledge", status "learned", confidence 0.7, each a passage of up to ~1,200
    characters that says where it came from ("From “Title”: …") and which characters of the original it is
    (span "chars:a-b"). Retrieval finds them like any memory and labels them "learned".
  * Learned material never becomes a fact about the owner's or the loved one's life: knowledge is left out of fact extraction,
    the "What I remember" list and the always-in-mind pack.
  * Forgetting a document hides all its passages (they can be brought back), like any memory.

Readers: .txt/.md as is; .html by stripping tags; .pdf with Apple PDFKit (memory/tools/pdftext.swift);
.docx/.doc/.rtf/.odt with macOS textutil. All on this Mac.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path

from core import web
from memory.store import Memory, MemoryStore

ROOT = Path(__file__).resolve().parents[1]
PDFTEXT = ROOT / "memory" / "tools" / "pdftext"
CHUNK = 1200
MAX_BYTES = 30 * 1024 * 1024
TEXTUTIL = {".docx", ".doc", ".rtf", ".odt", ".rtfd", ".webarchive"}
READABLE = {".txt", ".md", ".html", ".htm", ".pdf", *TEXTUTIL}

SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- document | web | music
  title TEXT NOT NULL,
  source TEXT NOT NULL,          -- original file name or web address
  original TEXT,                 -- where the kept original is, relative to the dataset root
  sha256 TEXT,
  status TEXT NOT NULL,          -- queued | done | failed | forgotten
  passages INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  requested_by TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
"""


def html_text(raw: str, base_url: str = "") -> tuple[str, str]:
    """(title, readable text) of an HTML page: menus, footers and scripts dropped (core/web.py)."""
    page = web.readable(raw, base_url)
    return page.title, page.text


def read_text(path: Path) -> str:
    """The text of a document, read on this Mac."""
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        return path.read_text(errors="replace")
    if ext in (".html", ".htm"):
        return html_text(path.read_text(errors="replace"))[1]
    if ext == ".pdf":
        if not PDFTEXT.exists():
            subprocess.run(["swiftc", "-O", str(PDFTEXT) + ".swift", "-o", str(PDFTEXT)], check=True, capture_output=True)
        r = subprocess.run([str(PDFTEXT), str(path)], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise ValueError("couldn't read this PDF")
        if not r.stdout.strip():
            raise ValueError("this PDF has no text in it (a scan?): not supported yet")
        return r.stdout
    if ext in TEXTUTIL:
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise ValueError(f"couldn't read this {ext} file")
        return r.stdout
    raise ValueError(f"can't read {ext or 'this kind of'} files yet")


def passages(text: str, size: int = CHUNK) -> list[tuple[int, int, str]]:
    """(start, end, text) pieces of up to `size` characters, cut at paragraph or sentence ends where possible."""
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "").replace("\x0c", "\n\n"))
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(n, i + size)
        if j < n:   # back up to a paragraph break, else a sentence end, else a space
            cut = max(text.rfind("\n\n", i + size // 2, j), -1)
            if cut == -1:
                cut = max(text.rfind(". ", i + size // 2, j) + 1, -1)
            if cut <= 0:
                cut = text.rfind(" ", i + size // 2, j)
            j = cut if cut > i else j
        piece = re.sub(r"\s*\n\s*", " ", text[i:j]).strip()
        if len(piece) > 40:   # skip crumbs (page numbers, lone headings)
            out.append((i, j, piece))
        i = j
    return out


class Learner:
    def __init__(self, store: MemoryStore, dataset_root: Path):
        self.store, self.root = store, dataset_root
        store.db.executescript(SCHEMA)

    # ---- acquire: keep the original ----
    def _keep(self, data: bytes, ext: str) -> tuple[str, str]:
        if not self.root.exists():
            raise RuntimeError("the dataset drive isn't connected: originals must be kept before learning")
        digest = hashlib.sha256(data).hexdigest()
        rel = f"raw/learned/{time.strftime('%Y')}/{digest}{ext}"
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            with open(f, "xb") as fh:   # never overwrite an original
                fh.write(data)
            f.chmod(0o444)
        from memory import archive
        archive._record(self.store, rel, digest, "learned", f)
        return rel, digest

    def _job(self, kind: str, title: str, source: str, rel: str | None, digest: str, by: str) -> int:
        old = self.store.db.execute("SELECT id FROM learn_jobs WHERE sha256=? AND status IN ('queued','done')", (digest,)).fetchone()
        if old:
            return old[0]   # the same document again: nothing new to learn
        now = time.time()
        with self.store.db:
            cur = self.store.db.execute(
                "INSERT INTO learn_jobs(kind,title,source,original,sha256,status,requested_by,created,updated) "
                "VALUES (?,?,?,?,?,'queued',?,?,?)", (kind, title[:200], source[:500], rel, digest, by, now, now))
        return cur.lastrowid

    def add_file(self, name: str, data: bytes, title: str = "", by: str = "owner") -> int:
        ext = Path(name).suffix.lower()
        if ext not in READABLE:
            raise ValueError(f"can't read {ext or 'this kind of'} files yet (I can read: {', '.join(sorted(READABLE))})")
        if len(data) > MAX_BYTES:
            raise ValueError("that file is too big (30 MB at most)")
        rel, digest = self._keep(data, ext)
        return self._job("document", title or Path(name).stem, name, rel, digest, by)

    def add_web(self, url: str, title: str = "", by: str = "owner") -> int:
        if not re.match(r"^https?://", url.strip()):
            raise ValueError("a web address starts with http:// or https://")
        f = web.fetch(url, max_bytes=MAX_BYTES)   # public addresses only, each redirect re-checked
        ext = ".pdf" if f.content_type == "application/pdf" else ".txt" if f.content_type == "text/plain" else ".html"
        rel, digest = self._keep(f.body, ext)
        if not title and ext == ".html":
            title = html_text(f.text, f.final_url)[0]
        return self._job("web", title or f.final_url, url.strip(), rel, digest, by)

    def known(self, source: str) -> dict | None:
        """The finished learn job for this source (e.g. a song already listened to), if any."""
        digest = hashlib.sha256(source.encode()).hexdigest()
        r = self.store.db.execute("SELECT * FROM learn_jobs WHERE sha256=? AND status='done'", (digest,)).fetchone()
        return dict(r) if r else None

    def add_learned(self, kind: str, title: str, source: str, pieces: list[str], by: str = "owner") -> int:
        """Save notes Echo made himself (e.g. what he heard in a song; core/music.py) as learned knowledge. No original
        file is kept; forget/bring back work like any learned document."""
        digest = hashlib.sha256(source.encode()).hexdigest()
        jid = self._job(kind, title, source, None, digest, by)
        n = 0
        for i, piece in enumerate(p for p in pieces if p.strip()):
            m, dup = self.store.add(Memory(kind="knowledge", status="learned", confidence=0.7, source=f"learned:{jid}",
                                           span=f"note:{i}", content=piece[:2000]))
            n += not dup
        self._set(jid, status="done", passages=n, error=None)
        return jid

    # ---- read → split → save ----
    def process_pending(self, limit: int = 1) -> int:
        """Read queued jobs (in quiet time). Returns how many were finished."""
        rows = self.store.db.execute("SELECT * FROM learn_jobs WHERE status='queued' AND original IS NOT NULL ORDER BY id LIMIT ?", (limit,)).fetchall()
        for job in rows:
            try:
                text = read_text(self.root / job["original"])
                n = 0
                for a, b, piece in passages(text):
                    m, dup = self.store.add(Memory(
                        kind="knowledge", status="learned", confidence=0.7, source=f"learned:{job['id']}",
                        span=f"chars:{a}-{b}", content=f"From “{job['title']}”: {piece}"[:2000]))
                    n += not dup
                self._set(job["id"], status="done", passages=n, error=None)
            except Exception as e:   # a damaged or unreadable file: said plainly, nothing half-saved is hidden
                self._set(job["id"], status="failed", error=str(e)[:300])
        return len(rows)

    def _set(self, jid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.db:
            self.store.db.execute(f"UPDATE learn_jobs SET {cols}, updated=? WHERE id=?", (*fields.values(), time.time(), jid))

    # ---- see and forget ----
    def jobs(self) -> list[dict]:
        return [dict(r) for r in self.store.db.execute("SELECT * FROM learn_jobs ORDER BY id DESC")]

    def forget(self, jid: int, by: str = "owner:panel") -> int:
        """Hide every passage learned from this job (restorable, like any forgotten memory). Returns how many."""
        ids = [r[0] for r in self.store.db.execute(
            "SELECT id FROM memories WHERE source=? AND hidden IS NULL", (f"learned:{jid}",))]
        for mid in ids:
            self.store.forget(mid, by=by, reason=f"forgot learned document {jid}")
        self._set(jid, status="forgotten")
        return len(ids)

    def restore(self, jid: int, by: str = "owner:panel") -> int:
        ids = [r[0] for r in self.store.db.execute(
            "SELECT id FROM memories WHERE source=? AND hidden IS NOT NULL", (f"learned:{jid}",))]
        for mid in ids:
            self.store.restore_hidden(mid, by=by)
        self._set(jid, status="done")
        return len(ids)
