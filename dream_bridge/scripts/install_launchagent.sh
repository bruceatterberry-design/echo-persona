#!/bin/zsh
# Start the Echo Dream Bridge (Mac side) at login and keep it running. Undo: uninstall_launchagent.sh
set -euo pipefail
HERE="${0:A:h:h}"
LABEL=com.echopersona.dreambridge
LA="$HOME/Library/LaunchAgents"; UIDN=$(id -u)
mkdir -p "$LA" "${HERE:h}/logs"
cd "$HERE" && uv sync --quiet
cat > "$LA/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HERE/.venv/bin/python</string><string>-m</string><string>bridge</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>${HERE:h}/logs/dreambridge.log</string>
  <key>StandardErrorPath</key><string>${HERE:h}/logs/dreambridge.log</string>
</dict></plist>
PLIST
plutil -lint -s "$LA/$LABEL.plist"
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UIDN" "$LA/$LABEL.plist"
echo "Dream bridge started. Status: $HERE/.venv/bin/python -m bridge status"
