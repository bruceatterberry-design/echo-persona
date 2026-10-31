#!/bin/zsh
# Copy the Echo dataset from the external SSD to the internal drive (which Time Machine backs up),
# then verify every voice take against its recorded sha256. Never deletes anything in the backup.
set -e
SRC="$(cd "${0:A:h:h}" && uv run python -c 'from core.config import load_config as l; c = l(); print(c.resolve(c.dataset_root))')"/   # dataset_root in config/echo.yaml
DST="${0:A:h}/../data/dataset-backup/"
[[ -d "$SRC" ]] || { echo "external SSD not connected: $SRC"; exit 1; }
mkdir -p "$DST"
rsync -a --exclude 'incoming/' --exclude 'staging/' "$SRC" "$DST"
cd "${0:A:h}/.." && PYTHONPATH=. uv run python - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path("data/dataset-backup")
ok = bad = 0
for m in root.glob("raw/voice/*/*.meta.json"):
    meta = json.loads(m.read_text())
    if hashlib.sha256(m.with_name(meta["take"] + ".wav").read_bytes()).hexdigest() == meta["sha256"]: ok += 1
    else: bad += 1; print("MISMATCH", m)
for m in root.glob("raw/phone/*/manifest.json"):   # phone sessions (Echo Persona Collector)
    for t in json.loads(m.read_text())["takes"]:
        f = m.parent / t["file"]
        if hashlib.sha256(f.read_bytes()).hexdigest() == t["sha256"]: ok += 1
        else: bad += 1; print("MISMATCH", f)
for side in root.glob("raw/conversations/*/*.jsonl.xz.sha256"):   # archived conversation days (memory/archive.py)
    digest, name = side.read_text().split()
    if hashlib.sha256((side.parent / name).read_bytes()).hexdigest() == digest: ok += 1
    else: bad += 1; print("MISMATCH", side.parent / name)
print(f"backup verified: {ok} originals OK (studio + phone + conversation days), {bad} mismatched")
import time
(root / ".verified.json").write_text(json.dumps({"at": time.time(), "ok": ok, "mismatched": bad}))   # shown on the status page
raise SystemExit(1 if bad else 0)
PY
