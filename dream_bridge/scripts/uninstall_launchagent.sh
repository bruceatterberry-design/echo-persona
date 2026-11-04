#!/bin/zsh
# Stop the dream bridge and remove its login item. Keeps the pairing and all dream files.
LABEL=com.echopersona.dreambridge
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Dream bridge stopped and removed from login. Data kept in ~/EchoPersona/data/dream-bridge and the dream folder."
