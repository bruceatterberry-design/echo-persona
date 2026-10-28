#!/bin/zsh
# Give /Applications/Echo Persona.app your face as its icon (rounded square, like other Mac apps).
# Newer macOS reads app icons from Assets.car, so the default one is removed and the app re-signed.
set -e
ROOT="${0:A:h:h}"; A="/Applications/Echo Persona.app"; T=$(mktemp -d)
# your starred face on the "My face" page (or else the first one you marked "This is me")
CROP="$(cd "$ROOT" && uv run python - <<'PY2'
from core.config import load_config
from face.library import FaceLibrary
lib = FaceLibrary(load_config().resolve(load_config().dataset_root))
faces = [f for p in lib.load()["photos"].values() for f in p.get("faces", []) if f.get("status") == "owner"]
faces.sort(key=lambda f: not f.get("preferred"))
print(lib.crops / faces[0]["crop"] if faces else "")
PY2
)" || CROP=""
if [[ -f "$CROP" ]]; then
  (cd "$ROOT" && uv run python - "$CROP" "$T/icon.png" <<'PY'
import sys
from PIL import Image, ImageDraw
im = Image.open(sys.argv[1]).convert("RGB"); w, h = im.size; s = int(min(w, h) * 0.86)
im = im.crop(((w - s) // 2, int((h - s) * 0.35), (w - s) // 2 + s, int((h - s) * 0.35) + s)).resize((824, 824), Image.LANCZOS)
icon = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
mask = Image.new("L", (824, 824), 0); ImageDraw.Draw(mask).rounded_rectangle((0, 0, 823, 823), radius=185, fill=255)
icon.paste(im, (100, 100), mask); icon.save(sys.argv[2])
PY
  )
  cp "$T/icon.png" "$ROOT/data/echo-icon.png"
elif [[ -f "$ROOT/data/echo-icon.png" ]]; then cp "$ROOT/data/echo-icon.png" "$T/icon.png"
else echo "no face image available; keeping the default icon"; exit 0; fi
mkdir "$T/i.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$T/icon.png" --out "$T/i.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$T/icon.png" --out "$T/i.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$T/i.iconset" -o "$A/Contents/Resources/applet.icns"
rm -f "$A/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$A/Contents/Info.plist" 2>/dev/null || true
codesign --force --deep -s - "$A" 2>/dev/null
touch "$A"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$A"
killall Dock 2>/dev/null || true
rm -rf "$T"; echo "  icon set"
