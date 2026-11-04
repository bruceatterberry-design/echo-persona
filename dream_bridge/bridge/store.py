"""Where the Mac side keeps things.

- Identity + pairing key: ~/EchoPersona/data/dream-bridge/ (data/ is never in git; pair.json is chmod 600).
- Dream files: ~/EchoPersona/data/dream (small JSON text). Set DREAM_BRIDGE_DATA to move them, e.g. to the
  Samsung SSD; note a background service touching the SSD can stall on a macOS permission check (seen 2026-11-03).
  outbox/  memory batches sent to the tower
  inbox/   dream results that came back (waiting for the interpreter)
  journal.jsonl  one line per message: ids, types, outcome. No memory text.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

ECHO_DIR = Path(os.environ.get("ECHO_HOME", Path(__file__).resolve().parents[2]))
STATE_DIR = Path(os.environ.get("DREAM_BRIDGE_STATE", ECHO_DIR / "data" / "dream-bridge"))


def dream_root() -> Path:
    if p := os.environ.get("DREAM_BRIDGE_DATA"):
        return Path(p)
    return ECHO_DIR / "data" / "dream"


def _write_private(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def identity() -> dict:
    p = STATE_DIR / "identity.json"
    if p.exists():
        return json.loads(p.read_text())
    ident = {"device_id": str(uuid.uuid4()), "created": time.time()}
    _write_private(p, ident)
    return ident


def load_pair() -> dict | None:
    p = STATE_DIR / "pair.json"
    return json.loads(p.read_text()) if p.exists() else None


def save_pair(pair: dict) -> None:
    _write_private(STATE_DIR / "pair.json", pair)


def forget_pair() -> bool:
    p = STATE_DIR / "pair.json"
    if p.exists():
        p.unlink()
        return True
    return False


def dirs() -> dict[str, Path]:
    root = dream_root()
    out = {"root": root, "inbox": root / "inbox", "outbox": root / "outbox"}
    for d in out.values():
        d.mkdir(parents=True, exist_ok=True)
    return out


def journal(**rec) -> None:
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
    with open(dirs()["root"] / "journal.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def save_inbox(env: dict) -> Path:
    """Write durably (fsync file + folder) before anyone is told it's stored."""
    d = dirs()["inbox"]
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{env['task_type']}-{env['message_id']}.json"
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(env, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return p


def receipts() -> dict:
    p = dirs()["root"] / "receipts.json"
    return json.loads(p.read_text()) if p.exists() else {}


def add_receipt(dream_id: str, receipt: dict) -> None:
    r = receipts()
    r[dream_id] = receipt
    p = dirs()["root"] / "receipts.json"
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(r, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
