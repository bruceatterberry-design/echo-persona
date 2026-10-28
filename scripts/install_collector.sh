#!/bin/zsh
# Install Echo Persona Collector on the Android phone plugged in by USB (needs adb: brew install android-platform-tools).
set -e
#   scripts/install_collector.sh                         build from source (needs the Android SDK), then install
#   scripts/install_collector.sh ~/Downloads/EchoPersonaCollector.apk   install the APK from the GitHub release
ROOT="${0:A:h:h}"; ADB=$(command -v adb || echo ~/Library/Android/sdk/platform-tools/adb)
APK="${1:-$ROOT/collector-android/app/build/outputs/apk/debug/app-debug.apk}"
if [[ ! -f "$APK" ]]; then (cd "$ROOT/collector-android" && JAVA_HOME=/opt/homebrew/opt/openjdk@17 ./gradlew -q assembleDebug); fi
STATE=$($ADB get-state 2>/dev/null || true)
[[ "$STATE" == "device" ]] || { echo "No phone ready. Plug it in, turn on USB debugging, and tap Allow on the phone."; exit 1; }
$ADB install -r "$APK" && echo "Installed. Open \"Echo Persona Collector\" on the phone."
