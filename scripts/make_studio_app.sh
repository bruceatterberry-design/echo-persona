#!/bin/zsh
# Build /Applications/Voice Studio.app (opens the Voice Recording Studio), give it your face with a
# red mic badge as its icon, and put it on the Dock. Safe to run again.
set -e
ROOT="${0:A:h:h}"; A="/Applications/Voice Studio.app"; T=$(mktemp -d)
chmod +x "$ROOT/scripts/open_studio.sh"
rm -rf "$A"
osacompile -o "$A" -e "do shell script quoted form of \"$ROOT/scripts/open_studio.sh\" & \" > /dev/null 2>&1 &\""
if [[ -f "$ROOT/data/echo-icon.png" ]]; then
  (cd "$ROOT" && uv run python - "$ROOT/data/echo-icon.png" "$T/icon.png" <<'PY'
import sys
from PIL import Image, ImageDraw
icon = Image.open(sys.argv[1]).convert("RGBA"); d = ImageDraw.Draw(icon)
cx, cy, r = 760, 760, 190                                   # badge, bottom right
d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(214, 48, 49, 255), outline=(255, 255, 255, 255), width=18)
w = (255, 255, 255, 255)
d.rounded_rectangle((cx - 45, cy - 120, cx + 45, cy + 30), radius=45, fill=w)         # mic head
d.arc((cx - 85, cy - 60, cx + 85, cy + 75), start=0, end=180, fill=w, width=18)       # cradle
d.line((cx, cy + 75, cx, cy + 120), fill=w, width=18)                                  # stem
d.line((cx - 50, cy + 120, cx + 50, cy + 120), fill=w, width=18)                       # base
icon.save(sys.argv[2])
PY
  )
  mkdir "$T/i.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$T/icon.png" --out "$T/i.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$T/icon.png" --out "$T/i.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$T/i.iconset" -o "$A/Contents/Resources/applet.icns"
  rm -f "$A/Contents/Resources/Assets.car"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$A/Contents/Info.plist" 2>/dev/null || true
fi
codesign --force --deep -s - "$A" 2>/dev/null
touch "$A"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$A"
# Add to the Dock unless it's already there.
if ! defaults read com.apple.dock persistent-apps | grep -q "/Voice%20Studio.app"; then
  defaults write com.apple.dock persistent-apps -array-add \
    "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${A// /%20}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
fi
killall Dock 2>/dev/null || true
rm -rf "$T"; echo "  Voice Studio is on the Dock"
