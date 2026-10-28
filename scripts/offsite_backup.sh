#!/bin/zsh
# Make (or refresh) an ENCRYPTED copy of everything that matters for Echo on another drive, to keep outside the house.
#   ~/EchoPersona/scripts/offsite_backup.sh /Volumes/<YourDrive>
# Creates <drive>/Echo-Offsite.sparsebundle (AES-256, macOS's built-in disk images) the first time, asking you to
# choose a password; later runs ask for that password and only copy what changed. Contents: original recordings and
# photos (raw/, consent/), derived data, memories, personality, settings, background-service settings, and the code.
# The password is never stored. Without it the copy cannot be opened, so write it down somewhere safe.
set -e
DEST="${1:?usage: offsite_backup.sh /Volumes/<drive>}"
ROOT="${0:A:h:h}"; DATA="$(cd "${0:A:h:h}" && uv run python -c 'from core.config import load_config as l; c = l(); print(c.resolve(c.dataset_root))')"; IMG="$DEST/Echo-Offsite.sparsebundle"
[[ -d "$DEST" && -w "$DEST" ]] || { echo "Can't write to $DEST"; exit 1; }
[[ -d "$DATA" ]] || { echo "external SSD not connected: $DATA"; exit 1; }

askpass() {   # BACKUP_PASSWORD is only for automated testing; normally you type it
  if [[ -n "$BACKUP_PASSWORD" ]]; then print -rn -- "$BACKUP_PASSWORD"; return; fi
  read -rs "p?$1: " </dev/tty; echo >/dev/tty; print -rn -- "$p"
}

if [[ ! -d "$IMG" ]]; then
  P1=$(askpass "Choose a password for the off-site copy"); P2=$(askpass "Type it again")
  [[ "$P1" == "$P2" && -n "$P1" ]] || { echo "Passwords didn't match."; exit 1; }
  NEED=$(( $(du -sk "$DATA" | cut -f1) / 1024 / 1024 + 20 ))
  print -rn -- "$P1" | hdiutil create -quiet -size "$(( NEED * 2 ))g" -type SPARSEBUNDLE -fs APFS -volname "Echo Offsite" \
    -encryption AES-256 -stdinpass "$IMG"
  PW="$P1"
else
  PW=$(askpass "Password for the off-site copy")
fi
MNT=$(print -rn -- "$PW" | hdiutil attach -stdinpass -nobrowse -noverify "$IMG" | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)
[[ -d "$MNT" ]] || { echo "Couldn't open the off-site copy (wrong password?)"; exit 1; }
trap 'hdiutil detach -quiet "$MNT" 2>/dev/null || true' EXIT

echo "Copying to the encrypted off-site copy…"
rsync -a --exclude 'incoming/' --exclude 'staging/' "$DATA/" "$MNT/EchoPersona-Data/"
mkdir -p "$MNT/EchoPersona-Mac"
SNAP=$(curl -s -X POST 127.0.0.1:8765/api/memory/backup | python3 -c "import json,sys; print(json.load(sys.stdin)['backup'])" 2>/dev/null || true)
[[ -n "$SNAP" && -f "$SNAP" ]] && cp "$SNAP" "$MNT/EchoPersona-Mac/memory.sqlite" || cp "$ROOT/data/memory.sqlite" "$MNT/EchoPersona-Mac/memory.sqlite"
rsync -a "$ROOT/data/personality/" "$MNT/EchoPersona-Mac/personality/"
rsync -a "$ROOT/config/" "$MNT/EchoPersona-Mac/config/"
mkdir -p "$MNT/EchoPersona-Mac/launchagents" && cp ~/Library/LaunchAgents/com.echopersona.*.plist "$MNT/EchoPersona-Mac/launchagents/"
(cd "$ROOT" && git bundle create -q "$MNT/EchoPersona-Mac/echo-code.bundle" --all)
cp "$ROOT/README.md" "$ROOT/docs/BACKUP_AND_RECOVERY.md" "$MNT/"

# verify every original recording in the copy against its checksum
OK=$(cd "$ROOT" && PYTHONPATH=. uv run python - "$MNT/EchoPersona-Data" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]); ok = bad = 0
for m in list(root.glob("raw/voice/*/*.meta.json")):
    meta = json.loads(m.read_text()); f = m.with_name(meta["take"] + ".wav")
    ok, bad = (ok + 1, bad) if hashlib.sha256(f.read_bytes()).hexdigest() == meta["sha256"] else (ok, bad + 1)
for m in root.glob("raw/phone/*/manifest.json"):
    for t in json.loads(m.read_text())["takes"]:
        f = m.parent / t["file"]
        ok, bad = (ok + 1, bad) if hashlib.sha256(f.read_bytes()).hexdigest() == t["sha256"] else (ok, bad + 1)
print(f"{ok} {bad}")
PY
)
read GOOD BAD <<< "$OK"
echo "Off-site copy verified: $GOOD recordings OK, $BAD mismatched."
python3 -c "import json,sys,time; json.dump({'at': time.time(), 'where': sys.argv[1], 'recordings_ok': int(sys.argv[2]), 'mismatched': int(sys.argv[3])}, open(sys.argv[4], 'w'))" \
  "$(basename "$DEST")" "$GOOD" "$BAD" "$ROOT/data/offsite-backup.json"
[[ "$BAD" == "0" ]] && echo "Done. Unplug the drive and keep it somewhere safe outside the house." || exit 1
