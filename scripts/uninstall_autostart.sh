#!/bin/zsh
# Undo install_autostart.sh. Does not touch any recordings, photos, memories or models.
for label in com.echopersona.core com.echopersona.stt com.echopersona.voice com.echopersona.ollama; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null; rm -f ~/Library/LaunchAgents/$label.plist && echo "removed $label"
done
osascript -e 'tell application "System Events" to if exists login item "Echo Persona" then delete login item "Echo Persona"' >/dev/null && echo "removed login item"
rm -rf "/Applications/Echo Persona.app" && echo "removed /Applications/Echo Persona.app"
