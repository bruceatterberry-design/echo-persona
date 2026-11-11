"""Scanned documents, kept on this Mac per person (data/documents/<owner|loved_one>/<id>/). Echo Link receives them; Core
reads them when someone asks Echo about one. the owner's and the loved one's are never mixed: every call names the person.

Each document folder: meta.json, original.pdf (as scanned on the device), searchable.pdf (+ invisible text),
text.json (page texts from Apple Vision, documents/tools/pdfread), thumb.jpg. Nothing leaves the Mac.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "documents"
PDFREAD = ROOT / "documents" / "tools" / "pdfread"
USERS = ("owner", "loved_one")
ID_RE = re.compile(r"^[a-f0-9]{12}$")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "this", "that", "what", "how", "do",
        "does", "i", "my", "me", "you", "your", "can", "with", "about", "where", "which", "are", "be", "find", "tell",
        "say", "says", "document", "manual", "page", "there", "from", "at", "by", "if", "when", "please"}


def _clean(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def _words(s: str) -> list[str]:
    return [w for w in _WORD.findall(s.lower()) if w not in STOP and len(w) > 1]


class Documents:
    def __init__(self, data: Path = DATA, reader: Path = PDFREAD):
        self.data, self.reader = data, reader
        self.lock = threading.Lock()

    def _dir(self, user: str, doc_id: str | None = None) -> Path:
        if user not in USERS:
            raise ValueError("unknown person")
        d = self.data / user
        if doc_id is not None:
            if not ID_RE.match(doc_id):
                raise ValueError("bad id")
            d = d / doc_id
        return d

    def _meta(self, d: Path) -> dict | None:
        try:
            return json.loads((d / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _save(self, d: Path, meta: dict) -> None:
        tmp = d / "meta.tmp"
        tmp.write_text(json.dumps(meta, indent=1))
        tmp.chmod(0o600)
        tmp.replace(d / "meta.json")

    # --- adding and reading ----------------------------------------------------------------------------------------
    def add(self, user: str, pdf: Path, name: str, folder: str = "", tags: list | None = None, sha256: str = "",
            wait: bool = False) -> dict:
        """Takes ownership of `pdf` (moved). Reading starts at once in the background."""
        doc_id = secrets.token_hex(6)
        d = self._dir(user, doc_id)
        d.mkdir(parents=True, mode=0o700)
        self.data.chmod(0o700)
        shutil.move(str(pdf), d / "original.pdf")
        (d / "original.pdf").chmod(0o600)
        meta = {"id": doc_id, "name": _clean(name, 80) or "Scan", "folder": _clean(folder, 40) or "Documents",
                "tags": [_clean(t, 30) for t in (tags or []) if _clean(t, 30)][:10], "pages": 0,
                "created": time.strftime("%Y-%m-%d %H:%M"), "size": (d / "original.pdf").stat().st_size,
                "sha256": sha256, "status": "reading"}
        self._save(d, meta)
        t = threading.Thread(target=self._read, args=(d,), daemon=True, name="pdfread")
        t.start()
        if wait:
            t.join()
            meta = self._meta(d)
        return meta

    def _read(self, d: Path) -> None:
        ok = False
        try:
            r = subprocess.run([str(self.reader), str(d / "original.pdf"), str(d)], capture_output=True, timeout=900)
            ok = r.returncode == 0 and (d / "text.json").exists()
        except (OSError, subprocess.SubprocessError):
            ok = False
        with self.lock:
            meta = self._meta(d) or {}
            if ok:
                pages = json.loads((d / "text.json").read_text())
                meta.update(status="ready", pages=len(pages), chars=sum(len(p["text"]) for p in pages))
                for f in d.iterdir():
                    f.chmod(0o600)
            else:
                meta["status"] = "failed"
            self._save(d, meta)

    def get(self, user: str, doc_id: str) -> dict | None:
        try:
            return self._meta(self._dir(user, doc_id))
        except ValueError:
            return None

    def pages(self, user: str, doc_id: str) -> list[dict]:
        try:
            return json.loads((self._dir(user, doc_id) / "text.json").read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def file(self, user: str, doc_id: str, which: str) -> Path | None:
        name = {"pdf": "searchable.pdf", "original": "original.pdf", "thumb": "thumb.jpg"}.get(which)
        try:
            p = self._dir(user, doc_id) / name if name else None
        except ValueError:
            return None
        return p if p and p.exists() else None

    def update(self, user: str, doc_id: str, name=None, folder=None, tags=None) -> dict | None:
        with self.lock:
            d = self._dir(user, doc_id)
            meta = self._meta(d)
            if not meta:
                return None
            if name is not None and _clean(name, 80):
                meta["name"] = _clean(name, 80)
            if folder is not None:
                meta["folder"] = _clean(folder, 40) or "Documents"
            if tags is not None:
                meta["tags"] = [_clean(t, 30) for t in tags if _clean(t, 30)][:10]
            self._save(d, meta)
            return meta

    def delete(self, user: str, doc_id: str) -> bool:
        d = self._dir(user, doc_id)
        if not d.exists():
            return False
        for f in d.iterdir():            # overwrite before removing (secure-ish delete on an SSD is best effort)
            try:
                f.write_bytes(b"\0" * min(f.stat().st_size, 50_000_000))
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)
        return True

    # --- finding -----------------------------------------------------------------------------------------------------
    def list(self, user: str, q: str = "") -> list[dict]:
        root = self._dir(user)
        out = []
        words = _words(q)
        for d in sorted(root.iterdir(), reverse=True) if root.exists() else []:
            meta = self._meta(d)
            if not meta:
                continue
            item = {k: meta.get(k) for k in ("id", "name", "folder", "tags", "pages", "created", "status")}
            if words:
                head = " ".join([meta["name"], meta["folder"], *meta["tags"]]).lower()
                text = "\n".join(p["text"] for p in self.pages(user, meta["id"]))
                low = text.lower()
                score = sum(3 for w in words if w in head) + sum(1 for w in words if w in low)
                if not score:
                    continue
                hit = min((low.find(w) for w in words if w in low), default=-1)
                item["snippet"] = _clean(text[max(0, hit - 60): hit + 120], 180) if hit >= 0 else ""
                item["score"] = score
            out.append(item)
        if words:
            out.sort(key=lambda x: -x["score"])
        return sorted(out, key=lambda x: x["created"], reverse=True) if not words else out

    def relevant(self, user: str, doc_id: str, question: str, budget: int = 5000) -> tuple[dict, list[dict]] | None:
        """The pages that best match the question (in page order), within `budget` characters; small documents whole."""
        meta = self.get(user, doc_id)
        if not meta or meta.get("status") != "ready":
            return (meta, []) if meta else None
        pages = self.pages(user, doc_id)
        if sum(len(p["text"]) for p in pages) <= budget:
            return meta, pages
        words = set(_words(question))
        scored = sorted(pages, key=lambda p: (-sum(p["text"].lower().count(w) for w in words), p["page"]))
        keep, used = [], 0
        for p in scored:
            if used + len(p["text"]) > budget and keep:
                continue
            keep.append({"page": p["page"], "text": p["text"][: budget - used]})
            used += len(keep[-1]["text"])
            if used >= budget:
                break
        return meta, sorted(keep, key=lambda p: p["page"])


def doc_note(meta: dict, pages: list[dict], who: str = "the owner") -> str:
    """One-time note for Echo (stripped from the history like the other notes; brackets in the text are softened)."""
    name = meta.get("name", "a document")
    if meta.get("status") == "reading":
        return f'[Document: "{name}" was just scanned and I am still reading it. Say so, and that it takes a minute.]'
    if not pages:
        return f'[Document: "{name}": I could not read any text in it. Say so honestly.]'
    body = "\n".join(f"--- page {p['page']} ---\n{p['text']}" for p in pages)
    body = body.replace("[", "(").replace("]", ")")
    whole = len(pages) >= meta.get("pages", 0)
    return (f'[Document: {who} scanned "{name}" ({meta.get("pages")} pages) and is asking about it. '
            f'{"Its whole text" if whole else "The pages that best match the question"} (read by the Mac, may have '
            f"small reading errors):\n{body}\nAnswer from this text and say which page. If the answer is not in it, "
            f"say so; don't make it up.]")
