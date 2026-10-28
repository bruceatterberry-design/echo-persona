"""Pick the best Voice Studio takes as voice references, so Echo can speak in your voice.

Run after recording in the Voice Studio (Keep the takes you like):
    uv run python scripts/make_voice_reference.py            # make references, use the storytelling one
    uv run python scripts/make_voice_reference.py factual    # ...or make another category the active one

For each of a few categories it picks one kept take that is 5–12 s long, clean (verdict good, best signal-to-noise) and was
read exactly as written, copies it to derived/voice/refs-v1/ref-<category>.wav with its transcript in refs.json, and sets
config/voice.json. Restart the voice service afterwards: launchctl kickstart -k gui/$(id -u)/com.echopersona.voice
Originals are never changed. Nothing leaves the Mac.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config  # noqa: E402

CATEGORIES = ("storytelling", "factual", "conversational", "calm")


def best_take(ds: Path, category: str) -> dict | None:
    prompts = {}
    for q in (ds / "derived" / "voice" / "qc-v1").glob("*/take-*.json"):
        r = json.loads(q.read_text())
        if (r.get("status") == "kept" and r.get("verdict") == "good" and 5 <= r.get("duration_s", 0) <= 12
                and r.get("read_match", 0) >= 0.95 and str(r.get("prompt_id", "")).startswith(category)
                and r.get("transcript")):
            prompts[q] = r
    if not prompts:
        return None
    q, r = max(prompts.items(), key=lambda kv: kv[1].get("snr_db", 0))
    wav = ds / "raw" / "voice" / r["session"] / f"{r['take']}.wav"
    return {"wav": wav, **r} if wav.exists() else None


def main(active: str = "storytelling", ds: Path | None = None, config_dir: Path = ROOT / "config") -> int:
    ds = ds or load_config().resolve(load_config().dataset_root)
    refs_dir = ds / "derived" / "voice" / "refs-v1"
    refs_dir.mkdir(parents=True, exist_ok=True)
    refs_path = refs_dir / "refs.json"
    refs = json.loads(refs_path.read_text()) if refs_path.exists() else {}
    for cat in CATEGORIES:
        t = best_take(ds, cat)
        if not t:
            print(f"  {cat}: no suitable kept take yet (5–12 s, good quality, read as written)")
            continue
        dest = refs_dir / f"ref-{cat}.wav"
        shutil.copyfile(t["wav"], dest)
        refs[cat] = {"source": str(t["wav"].relative_to(ds)), "source_sha256": hashlib.sha256(t["wav"].read_bytes()).hexdigest(),
                     "text": t["transcript"], "category": cat, "duration_s": t["duration_s"], "snr_db": t.get("snr_db")}
        print(f"  {cat}: {t['session']}/{t['take']} ({t['duration_s']:.1f} s, SNR {t.get('snr_db', 0):.0f} dB)")
    if not refs:
        print("No references made. Record and Keep more takes in the Voice Studio first.")
        return 1
    refs_path.write_text(json.dumps(refs, indent=2))
    if active not in refs:
        active = next(iter(refs))
    vpath = config_dir / "voice.json"
    voice = json.loads(vpath.read_text()) if vpath.exists() else {}
    vpath.write_text(json.dumps(voice | {"reference": active}, indent=2) + "\n")
    print(f"Active reference: {active}. Now restart the voice service:\n"
          "  launchctl kickstart -k gui/$(id -u)/com.echopersona.voice")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
