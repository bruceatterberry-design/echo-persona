# Echo Persona Collector (Android) — setup

The app records guided sessions (front-camera video + lossless voice) and keeps them on the phone until the Mac has
copied and verified them over a **USB cable**. The app has **no internet permission** (verified in the built APK:
only CAMERA and RECORD_AUDIO).

## One-time phone setup (about 2 minutes)
1. **Turn on Developer options:** Settings → About phone → tap **Build number** 7 times (Samsung: Settings → About phone →
   Software information → Build number). Enter your PIN if asked.
2. **Turn on USB debugging:** Settings → System → Developer options (Samsung: Settings → Developer options) → **USB debugging** on.
3. Plug the phone into the Mac with a USB cable. On the phone, tap **Allow** (tick "Always allow from this computer").
4. On the Mac: `~/EchoPersona/scripts/install_collector.sh` (or ask Claude to install it).

## Each day
- Open **Echo Persona Collector**, press **Start a session** (about 15 minutes, twice a day for 14 or 28 days).
- Recording only happens while you press Record on that screen; a red banner shows when the camera/mic is on.
- Reminders at 10:00 AM and 7:00 PM by default (tap the times on the home screen to change them). They skip a day once both sessions are done, or while collection is paused.
- Each session starts with 2 full-resolution photos (the front camera records video at only 640×480 on the phone).

## Copying to the Mac
Plug in the USB cable → Echo's **Avatar Collector** page → **Copy from phone**. Each session is pulled, every file is checked
against its SHA-256 fingerprint, stored read-only under `raw/phone/<session>/` on the external SSD, and a receipt is written
back to the phone. Interrupted copies are simply redone next time. The phone deletes media only if you turn on
"Free up space after the Mac has verified a copy", and only for files the Mac verified byte-for-byte.

## On-phone layout
`/sdcard/Android/data/com.echopersona.collector/files/` → `consent.json`, `sessions/<id>/take-NNN.(wav|mp4|json)`,
`sessions/<id>/manifest.json`, `receipts/<id>.json`.

## Build
`cd collector-android && JAVA_HOME=/opt/homebrew/opt/openjdk@17 ./gradlew assembleDebug` (AGP 8.7.3, Kotlin 2.0.21,
Compose BOM 2024.12.01, CameraX 1.4.1, compileSdk/targetSdk 35, minSdk 29). Toolchain: Homebrew openjdk@17, gradle,
android-commandlinetools; SDK at ~/Library/Android/sdk (platform-tools, platforms;android-35, build-tools;35.0.0).
