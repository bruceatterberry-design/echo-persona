#!/bin/zsh
# Open the Voice Recording Studio once Echo is awake. Used by the "Voice Studio" app.
ROOT="${0:A:h:h}"
for label in com.echopersona.ollama com.echopersona.voice com.echopersona.stt com.echopersona.core; do   # make sure the background parts are running
  launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$label.plist 2>/dev/null
done
for i in {1..180}; do curl -s --max-time 1 127.0.0.1:8765/health >/dev/null && break; sleep 1; done
# Same Chrome profile as Echo's window, so the mic permission is already remembered.
open -na "Google Chrome" --args --user-data-dir="$ROOT/data/chrome-echo" --no-first-run --no-default-browser-check \
  --app=http://127.0.0.1:8765/studio.html --window-size=1200,900
