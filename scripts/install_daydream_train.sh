#!/bin/zsh
# Run daydream training (scripts/daydream_train.py) every night at 02:30. It does nothing unless
# config/echo.yaml daydream.training.enabled is true, it is 01:00-05:00, nobody talked to Echo for 30 min,
# and there are enough kept examples. Undo: launchctl bootout gui/$(id -u)/com.echopersona.daydreamtrain
set -euo pipefail
ROOT="${0:A:h:h}"
if [[ ! -x "$ROOT/.venv-train/bin/python" ]]; then
  echo "First set up the training tools (about 6 GB with the model, downloaded on the first run):"
  echo "  cd $ROOT && uv venv .venv-train --python 3.12 && VIRTUAL_ENV=.venv-train uv pip install mlx-lm gguf"
  echo "and set daydream.training.enabled: true in config/echo.yaml."
  exit 1
fi
LABEL=com.echopersona.daydreamtrain; P="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/.venv/bin/python</string><string>$ROOT/scripts/daydream_train.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>30</integer></dict>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$ROOT/logs/daydreamtrain.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/daydreamtrain.log</string>
</dict></plist>
PLIST
plutil -lint -s "$P"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$P"
echo "Nightly daydream training scheduled (02:30)."
