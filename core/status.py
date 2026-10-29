"""One status report for whoever looks after Echo: services, the Mac, data, backups, speed, recent problems.
Read-only; never includes conversation content (the logs don't contain any)."""
from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

LABELS = {"com.echopersona.ollama": "Language model (Ollama)", "com.echopersona.voice": "Cloned voice",
          "com.echopersona.stt": "Speech recognition (Parakeet)", "com.echopersona.core": "Echo Persona Core"}
HEALTH = {"com.echopersona.ollama": "http://127.0.0.1:11434/api/version", "com.echopersona.voice": "http://127.0.0.1:8766/health",
          "com.echopersona.stt": "http://127.0.0.1:8767/health"}
def _run(*cmd: str, timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _get(url: str) -> dict | None:
    try:
        return json.loads(urllib.request.urlopen(url, timeout=2).read())
    except Exception:
        return None


def _age(ts: float | None) -> dict:
    if not ts:
        return {"at": None, "days": None}
    return {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), "days": round((time.time() - ts) / 86400, 1)}


def services() -> list[dict]:
    listed = {}
    for line in _run("launchctl", "list").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] in LABELS:
            listed[parts[2]] = {"pid": None if parts[0] == "-" else int(parts[0]), "last_exit": parts[1]}
    out = []
    for label, name in LABELS.items():
        l = listed.get(label)
        if label == "com.echopersona.core":
            ok, detail = True, "answering (you're reading this page)"
        else:
            h = _get(HEALTH[label])
            ok = bool(h) and (h.get("ok", True) is not False)
            detail = "ready" if ok else (h or {}).get("error") or ("starting" if l and l["pid"] else "not running")
        out.append({"name": name, "label": label, "ok": ok and bool(l), "running": bool(l and l["pid"]), "detail": detail,
                    "installed": bool(l)})
    return out


def mac(dataset_root: Path, root: Path) -> dict:
    free = re.search(r"free percentage: (\d+)%", _run("memory_pressure"))
    swap = re.search(r"used = ([\d.]+)M", _run("sysctl", "vm.swapusage"))
    internal = shutil.disk_usage("/")
    ssd_ok = dataset_root.is_dir()
    ssd = shutil.disk_usage(dataset_root) if ssd_ok else None
    ssd_info = _run("diskutil", "info", str(dataset_root.parent)) if ssd_ok else ""
    enc = re.search(r"FileVault:\s+(\w+)", ssd_info)
    return {"memory_free_pct": int(free.group(1)) if free else None, "swap_used_gb": round(float(swap.group(1)) / 1024, 1) if swap else None,
            "internal_free_gb": round(internal.free / 1e9), "ssd_connected": ssd_ok,
            "ssd_free_gb": round(ssd.free / 1e9) if ssd else None,
            "ssd_encrypted": (enc.group(1) == "Yes") if enc else None,
            "filevault": "On" in _run("fdesetup", "status")}


def data(store, dataset_root: Path, pdir: Path) -> dict:
    out = {"memories": len(store.facts()), "conversation_notes": len(store.summaries()),
           "conversation_turns": store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]}
    try:
        from voice.studio import Studio
        st = Studio(dataset_root).stats() if dataset_root.is_dir() else {}
        out["voice_minutes"] = st.get("kept_minutes")
        phone = dataset_root / "raw" / "phone"
        sessions = sorted(p.name for p in phone.iterdir() if (p / "receipt.json").exists()) if phone.is_dir() else []
        out["phone_sessions"] = len(sessions)
        out["last_phone_session"] = sessions[-1] if sessions else None
        from face.library import current_atlas
        atlas = current_atlas(dataset_root) / "atlas.json"
        out["face_frames"] = len(json.loads(atlas.read_text())["frames"]) if atlas.exists() else 0
    except Exception:
        pass
    prof = pdir / "profile.json"
    if prof.exists():
        p = json.loads(prof.read_text())
        out["approved_traits"] = len(p.get("traits", []))
        out["summary_lines_in_use"] = len(p.get("condensed") or [])
    return out


def backups(root: Path, dataset_root: Path) -> dict:
    v = root / "data/dataset-backup/.verified.json"
    ver = json.loads(v.read_text()) if v.exists() else {}
    snaps = sorted((root / "data/backups").glob("memory-*.sqlite"), key=lambda p: p.stat().st_mtime)
    off = root / "data/offsite-backup.json"
    offsite = json.loads(off.read_text()) if off.exists() else {}
    return {"dataset_copy": _age(ver.get("at")) | {"recordings_ok": ver.get("ok"), "mismatched": ver.get("mismatched")},
            "memory_snapshot": _age(snaps[-1].stat().st_mtime if snaps else None),
            "offsite": _age(offsite.get("at")) | {"where": offsite.get("where")}}


def speed_and_problems(root: Path) -> dict:
    log = root / "logs" / "echo.log"
    lines = log.read_text(errors="ignore").splitlines()[-4000:] if log.exists() else []
    first = [int(m.group(1)) for l in lines if (m := re.search(r"first_token_ms=(\d+)", l))][-30:]
    problems = [re.sub(r"^(\S+ \S+),\d+ ", r"\1 ", l) for l in lines if " WARNING " in l or " ERROR " in l][-8:]
    return {"first_word_ms_median": round(statistics.median(first)) if first else None,
            "first_word_ms_max": max(first) if first else None, "replies_measured": len(first), "recent_problems": problems}


def report(store, stt, root: Path, dataset_root: Path, pdir: Path) -> dict:
    sv = services()
    m = mac(dataset_root, root)
    sp = speed_and_problems(root)
    sp["speech_recognition"] = getattr(stt, "name", "?")
    sp["fallbacks_to_whisper"] = getattr(stt, "fallbacks", 0)
    b = backups(root, dataset_root)
    attention = []
    if not all(s["ok"] for s in sv):
        attention.append("A part of Echo isn't running: see Services.")
    if not m["ssd_connected"]:
        attention.append("The data folder (dataset_root) isn't available: the cloned voice and face need it.")
    if m["memory_free_pct"] is not None and m["memory_free_pct"] < 10:
        attention.append("The Mac is low on memory. Quit other apps, or restart the Mac.")
    if m["internal_free_gb"] < 15:
        attention.append("The Mac's internal drive is getting full.")
    days = b["dataset_copy"]["days"]
    if days is None or days > 14:
        attention.append("The recordings backup is more than 2 weeks old: run scripts/backup_dataset.sh.")
    if b["dataset_copy"].get("mismatched"):
        attention.append("The last backup check found damaged copies: run scripts/backup_dataset.sh again.")
    if m["ssd_connected"] and m["ssd_encrypted"] is False:
        attention.append("The external SSD isn't encrypted (see SECURITY.md).")
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "attention": attention, "services": sv, "mac": m,
            "data": data(store, dataset_root, pdir), "backups": b, "speed": sp}
