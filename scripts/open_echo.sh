#!/bin/zsh
# Open Echo's window once it's awake. Used by the Echo app and the login item.
ROOT="${0:A:h:h}"
for label in com.echopersona.ollama com.echopersona.voice com.echopersona.stt com.echopersona.core; do   # make sure the background parts are running
  launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$label.plist 2>/dev/null
done
osascript -e 'display notification "Echo is waking up…" with title "Echo"' 2>/dev/null
for i in {1..180}; do curl -s --max-time 1 127.0.0.1:8765/health >/dev/null && break; sleep 1; done
# Chrome "app" window (no tabs or address bar) snapped to the LEFT HALF of the main screen. Not full screen:
# The owner found full screen breaks the facial movement. Own profile (remembers the mic permission), and allowed to
# play Echo's voice without a click first.
read SW SH <<< "$(osascript -e 'tell application "Finder" to get bounds of window of desktop' | awk -F', ' '{print $3, $4}')"
SW=${SW:-2560}; SH=${SH:-1080}
open -na "Google Chrome" --args --user-data-dir="$ROOT/data/chrome-echo" --no-first-run --no-default-browser-check \
  --app="http://127.0.0.1:8765/?v=$(date +%s)" --window-position=0,0 --window-size=$((SW / 2)),$SH --autoplay-policy=no-user-gesture-required
