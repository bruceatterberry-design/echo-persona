# Troubleshooting

Start with the simple fixes: **close Echo's window and click the Echo icon in the Dock.** If that doesn't help,
**restart the Mac**; everything starts by itself.

| What you notice | Likely cause | What to do |
|---|---|---|
| Window says "connecting" for a long time | Echo Persona Core is starting or stopped | Wait 1 minute after login. Then: `launchctl kickstart -k gui/$(id -u)/com.echopersona.core` |
| Top right says "LLM offline" | Ollama not running | `launchctl kickstart -k gui/$(id -u)/com.echopersona.ollama` |
| Echo talks in a robotic voice and says its own voice isn't available | Voice service down, or the SSD isn't plugged in | Plug in the external SSD; the voice retries every 30 s. Check `curl -s 127.0.0.1:8766/health` |
| Echo shows a simple line drawing instead of the owner's face | SSD not connected (the face frames live there) | Plug in the SSD, then reopen the window |
| Echo mishears words, or listening feels slow | Parakeet service down (Echo then uses Whisper automatically) | `curl -s 127.0.0.1:8767/health`; `launchctl kickstart -k gui/$(id -u)/com.echopersona.stt`. To go back to Whisper for good: `"stt": "whisper"` in `config/voice.json`, restart `com.echopersona.core`. Full restore point: `data/backups/20261027-before-parakeet/RESTORE.txt` (also on the SSD under Echo-Backups) |
| Echo doesn't react to "Hey Echo" | Listen mode not on, mic blocked, or another window has the mic | Top left must show red **MIC ON**. Check Listen = "Hey Echo". Close other Echo windows. In Chrome, allow the microphone for 127.0.0.1 |
| Echo answers things nobody said to it | Conversation mode still on, or TV/other voices | Say "that's all"; it also ends after 20 s of quiet |
| Mouth or face movement looks broken | Window is full screen | Use a normal window (it opens on the left half) |
| First reply after starting is slow (≈ 10–15 s) | Models loading and reading its instructions | Normal once per start (it happens in the background); after that it starts answering in well under a second |
| Status page shows a Claude helper problem | Logged out of Claude Code, plan limit reached, or no internet | Nothing breaks: the job was done on the Mac instead. To fix: run `claude` in Terminal and log in, or switch the helper off on the Status page |
| "Copy from phone" says no phone found | Cable, USB debugging, or "Allow" not tapped | See ANDROID_SETUP.md; on the phone tap Allow for USB debugging |
| A phone session isn't copied ("not finished yet") | App was closed mid-session | Open Echo Persona Collector once: it closes open sessions; then copy again |
| A reminder didn't appear on the phone | Notifications blocked, paused, or today's two sessions are done | Phone Settings → Apps → Echo Persona Collector → Notifications on |
| Echo window icon is the default script icon | macOS icon cache | `~/EchoPersona/scripts/set_app_icon.sh`, or drag the icon off the Dock and back |

## Where to look
- Logs: `~/EchoPersona/logs/` (`echo.log`, `com.echopersona.core.log`, `com.echopersona.voice.log`, `com.echopersona.ollama.log`).
  They never contain conversation text.
- Status of everything: `curl -s 127.0.0.1:8765/health` and `curl -s 127.0.0.1:8766/health`.
- What's been done and known issues: `~/EchoPersona/STATUS.md`.
- Run the tests: `cd ~/EchoPersona && uv run pytest -q` (should all pass).

## Things that are deliberately not possible
- Echo can't be reached from another computer or the internet (by design).
- Original recordings can't be edited or overwritten (read-only, by design).
- It won't save a memory on its own; someone has to say "remember that…" or use the memory panel.
