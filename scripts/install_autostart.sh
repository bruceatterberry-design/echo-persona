#!/bin/zsh
# Make Echo start by itself: 4 per-user LaunchAgents (Ollama, the cloned voice, speech recognition, Echo Persona Core) that start at login
# and restart if they crash, an "Echo Persona" app in /Applications, and a login item that opens its window.
# Everything listens on 127.0.0.1 only. Undo with scripts/uninstall_autostart.sh.
set -e
ROOT="${0:A:h:h}"; LA=~/Library/LaunchAgents; UIDN=$(id -u); UV=/opt/homebrew/bin/uv
mkdir -p "$LA" "$ROOT/logs"

agent() {  # label, workdir, env-xml, program args...
  local label=$1 wd=$2 envxml=$3; shift 3
  local args=""; for a in "$@"; do args+="<string>$a</string>"; done
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$wd</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>$envxml
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$ROOT/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/$label.log</string>
</dict></plist>
PLIST
  plutil -lint -s "$LA/$label.plist"
  launchctl bootout "gui/$UIDN/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UIDN" "$LA/$label.plist"
  echo "  started $label"
}

kv() { echo "<key>$1</key><string>$2</string>"; }
agent com.echopersona.ollama "$ROOT" "$(kv OLLAMA_HOST 127.0.0.1:11434)$(kv OLLAMA_FLASH_ATTENTION 1)$(kv OLLAMA_KV_CACHE_TYPE q8_0)$(kv OLLAMA_KEEP_ALIVE 24h)" \
  /opt/homebrew/opt/ollama/bin/ollama serve
agent com.echopersona.voice "$ROOT/tts" "$(kv HF_HUB_OFFLINE 1)$(kv HF_HUB_DISABLE_TELEMETRY 1)" \
  "$UV" run --frozen python service.py
agent com.echopersona.stt "$ROOT/tts" "$(kv HF_HUB_OFFLINE 1)$(kv HF_HUB_DISABLE_TELEMETRY 1)" \
  "$UV" run --frozen python stt_service.py
agent com.echopersona.core "$ROOT" "$(kv PYTHONPATH "$ROOT")" \
  "$UV" run --frozen python -m core.server

# The Echo app: waits until Echo is awake, then opens its window (Chrome app window, own profile).
APP="/Applications/Echo Persona.app"
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script quoted form of \"$ROOT/scripts/open_echo.sh\" & \" > /dev/null 2>&1 &\""
"$ROOT/scripts/set_app_icon.sh"   # your face as the icon (once you've approved one)
echo "  created $APP"

# Open Echo's window at login.
osascript -e 'tell application "System Events" to if not (exists login item "Echo Persona") then make login item at end with properties {path:"'"$APP"'", hidden:false}' >/dev/null
echo "  added login item: Echo"
