"""Copy Echo Persona Collector recordings from the Android phone over USB (adb), verify, and receipt them.

Per session: pull to staging/phone/<id>.partial → check every take's sha256 against its metadata → move to
raw/phone/<id>/ (read-only) → push receipts/<id>.json back to the phone. Idempotent and resumable: a session is
done only when its verified copy exists; an interrupted pull leaves nothing behind but a .partial folder that the
next run replaces. No network is used.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

PKG = "com.echopersona.collector"
PHONE_FILES = f"/sdcard/Android/data/{PKG}/files"
ADB = shutil.which("adb") or str(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb")
SESSION_RE = re.compile(r"^\d{8}-\d{6}x*$")
MEDIA_EXT = {".wav", ".mp4", ".jpg"}


class SyncError(Exception):
    pass


class Phone(Protocol):
    def devices(self) -> list[str]: ...
    def list_sessions(self) -> list[str]: ...
    def has_manifest(self, sid: str) -> bool: ...
    def pull_session(self, sid: str, dest: Path) -> None: ...
    def push_receipt(self, sid: str, src: Path) -> None: ...


class AdbPhone:
    def __init__(self, adb: str = ADB):
        self.adb = adb

    def _run(self, *args: str, timeout: int = 60) -> str:
        try:
            r = subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise SyncError("adb not installed") from e
        except subprocess.TimeoutExpired as e:
            raise SyncError("the phone stopped responding") from e
        if r.returncode != 0:
            raise SyncError((r.stderr or r.stdout).strip()[:300] or "adb failed")
        return r.stdout

    def devices(self) -> list[str]:
        out = self._run("devices")
        lines = [l.split("\t") for l in out.splitlines()[1:] if "\t" in l]
        unauthorized = [d for d, s in lines if s == "unauthorized"]
        if unauthorized and not any(s == "device" for _, s in lines):
            raise SyncError("On the phone, tap \"Allow\" on the USB debugging question, then try again.")
        return [d for d, s in lines if s == "device"]

    def list_sessions(self) -> list[str]:
        out = self._run("shell", f"ls {PHONE_FILES}/sessions 2>/dev/null || true")
        return [s.strip() for s in out.split() if SESSION_RE.match(s.strip())]

    def has_manifest(self, sid: str) -> bool:
        return "yes" in self._run("shell", f"[ -f {PHONE_FILES}/sessions/{sid}/manifest.json ] && echo yes || echo no")

    def pull_session(self, sid: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("pull", f"{PHONE_FILES}/sessions/{sid}", str(dest), timeout=3600)

    def push_receipt(self, sid: str, src: Path) -> None:
        self._run("shell", f"mkdir -p {PHONE_FILES}/receipts")
        self._run("push", str(src), f"{PHONE_FILES}/receipts/{sid}.json")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_session(folder: Path) -> dict[str, str]:
    """Check every take against its metadata. Returns {file name: sha256}. Raises SyncError on any problem."""
    mf = folder / "manifest.json"
    if not mf.exists():
        raise SyncError("manifest missing")
    manifest = json.loads(mf.read_text())
    verified = {}
    for take in manifest["takes"]:
        name = take["file"]
        if "/" in name or "\\" in name or name.startswith(".") or Path(name).suffix not in MEDIA_EXT:
            raise SyncError(f"unexpected file name {name!r}")
        p = folder / name
        if not p.exists():
            raise SyncError(f"{name} is missing")
        if p.stat().st_size != take["bytes"] or sha256_file(p) != take["sha256"]:
            raise SyncError(f"{name} doesn't match its fingerprint")
        verified[name] = take["sha256"]
    return verified


def sync(phone: Phone, dataset_root: str | Path) -> dict:
    root = Path(dataset_root)
    if not root.is_dir():
        raise SyncError("external SSD not connected")
    devs = phone.devices()
    if not devs:
        raise SyncError("No phone found. Plug it in with a USB cable and make sure USB debugging is on.")
    if len(devs) > 1:
        raise SyncError("More than one Android device is connected. Unplug the others.")
    raw, staging = root / "raw" / "phone", root / "staging" / "phone"
    result = {"copied": [], "already_had": [], "unfinished": [], "failed": {}, "bytes": 0, "seconds": 0.0}
    t0 = time.time()
    for sid in phone.list_sessions():
        final = raw / sid
        if (final / "receipt.json").exists():   # verified earlier: just make sure the phone has its receipt
            phone.push_receipt(sid, final / "receipt.json")
            result["already_had"].append(sid)
            continue
        if not phone.has_manifest(sid):
            result["unfinished"].append(sid)    # still recording, or abandoned without any kept takes
            continue
        part = staging / f"{sid}.partial"
        shutil.rmtree(part, ignore_errors=True)
        try:
            phone.pull_session(sid, part)
            verified = verify_session(part)
        except SyncError as e:
            result["failed"][sid] = str(e)
            continue
        receipt = {"session": sid, "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "verified": verified,
                   "stored_at": str(final.relative_to(root))}
        (part / "receipt.json").write_text(json.dumps(receipt, indent=2))
        final.parent.mkdir(parents=True, exist_ok=True)
        part.rename(final)                      # same drive: atomic
        for f in final.iterdir():
            f.chmod(0o444)
        final.chmod(0o555)
        phone.push_receipt(sid, final / "receipt.json")
        result["copied"].append(sid)
        result["bytes"] += sum(f.stat().st_size for f in final.iterdir())
    result["seconds"] = round(time.time() - t0, 1)
    return result


def status(dataset_root: str | Path) -> dict:
    raw = Path(dataset_root) / "raw" / "phone"
    sessions = sorted(p.name for p in raw.iterdir() if (p / "receipt.json").exists()) if raw.is_dir() else []
    takes = kept_ms = 0
    for sid in sessions:
        for t in json.loads((raw / sid / "manifest.json").read_text())["takes"]:
            takes += 1
            kept_ms += t.get("duration_ms", 0) if t.get("status") == "kept" else 0
    return {"sessions": len(sessions), "takes": takes, "kept_minutes": round(kept_ms / 60000, 1),
            "last": sessions[-1] if sessions else None}
