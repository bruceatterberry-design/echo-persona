#!/bin/zsh
# Render Echo's talking face from your starred photo (the "My face" page), so the window shows you instead of a drawing.
# Needs LivePortrait set up in animate/ (docs/PC_SETUP.md). Takes about 10 minutes on an M-series Mac; runs offline.
# Then reload Echo's window (Cmd+R).
set -e
ROOT="${0:A:h:h}"
PY="$ROOT/animate/.venv/bin/python"
[[ -x "$PY" ]] || { echo "LivePortrait isn't set up in $ROOT/animate yet (see docs/PC_SETUP.md)."; exit 1; }
read PHOTO OUT <<< "$(cd "$ROOT" && uv run python - <<'PY2'
from core.config import load_config
from face.library import FaceLibrary
lib = FaceLibrary(load_config().resolve(load_config().dataset_root))
photos = [p for p in lib.load()["photos"].values() if p.get("preserved")
          and any(f.get("status") == "owner" for f in p.get("faces", []))]
photos.sort(key=lambda p: not any(f.get("preferred") for f in p["faces"]))
print((lib.root / photos[0]["preserved"]) if photos else "-", lib.atlas)
PY2
)"
[[ "$PHOTO" != "-" ]] || { echo "No approved photo yet: open the \"My face\" page, mark your face \"This is me\" and star the best one."; exit 1; }
echo "Rendering from $PHOTO into $OUT (about 10 minutes)…"
mkdir -p "$OUT"
PYTORCH_ENABLE_MPS_FALLBACK=1 "$PY" "$ROOT/face/render_face.py" atlas5 "$PHOTO" "$OUT"
echo "Done. Reload Echo's window (Cmd+R) to see your face."
