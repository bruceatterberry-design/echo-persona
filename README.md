# Echo Persona

A private, local "digital self" for your Mac: a talking companion that speaks in **your voice**, shows **your face**,
remembers what you tell it, and talks the way you do. It's made to be left for someone you love.

Everything runs on your own Mac. Recordings, photos, memories and conversations never leave it, and nothing is
uploaded or sent to any outside service. (The one optional exception, switched off by default, is described under Privacy.)

- **Talks and listens:** say "Hey Echo, …" or use push-to-talk. Local speech recognition (Parakeet / Whisper) and a
  local language model (Llama 3.1 8B via Ollama). Replies stream, so it starts speaking within a few seconds.
- **Your voice:** record about 30 minutes in the built-in **Voice Studio**; Echo clones your voice from your best takes
  (Qwen3-TTS, zero-shot, no training run needed).
- **Your face:** add a few photos; Echo renders a talking, blinking face from one of them (LivePortrait, offline).
  The mouth follows the voice as it plays.
- **Remembers:** say "remember that …" or use the memory panel. Nothing is saved without being asked.
- **Talks like you:** the "How I talk" page proposes personality traits from your own recorded answers, each backed by a
  quote; only the ones you approve are used.
- **Opens web pages:** "Hey Echo, open the Weather Channel" or "go to weather dot com".
- **Android Collector app (optional):** guided daily sessions that record your voice and face video on your phone, and
  copy to the Mac over a USB cable only (the app has no internet permission).

> Echo Persona is a personal project, not a product. It is honest about being a digital self built from what you chose to
> share, not a living person.

## Requirements
- A Mac with Apple Silicon (M1 or later) and **16 GB of memory or more**. Tested on a Mac mini M4 with 16 GB.
- About 20 GB free for models, plus room for your recordings. An external SSD for the data is recommended.
- Google Chrome (Echo's window is a Chrome app window).
- [Homebrew](https://brew.sh).
- Optional: an Android phone (Android 8+) for the Collector app.

## Install
```sh
git clone https://github.com/bruceatterberry-design/echo-persona.git ~/EchoPersona
cd ~/EchoPersona
brew install uv ffmpeg ollama
uv sync && (cd tts && uv sync)
brew services start ollama   # or: ollama serve
ollama pull llama3.1:8b
scripts/build_face_tools.sh       # Apple Vision face finder (Swift)
```
Download the speech and voice models once (about 5 GB; Echo runs offline afterwards):
```sh
cd ~/EchoPersona/tts
HF_HOME=../models/hf uv run python -c "from huggingface_hub import snapshot_download as d; \
  [d(r) for r in ('mlx-community/parakeet-tdt-0.6b-v2', 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16')]"
```
Settings: copy `config/echo.example.yaml` to `config/echo.yaml` and set where your data lives (`dataset_root`,
for example a folder on an external SSD) and the name of the person Echo will mostly talk with. Copy
`config/personality.example.yaml` to `config/personality.yaml` and fill in the parts in `<angle brackets>`.

Then make Echo start by itself at login and add **Echo Persona** to /Applications:
```sh
scripts/install_autostart.sh
```
Open **Echo Persona** and click **Allow** when Chrome asks for the microphone. To undo: `scripts/uninstall_autostart.sh`
(your data is untouched).

More detail: [docs/PC_SETUP.md](docs/PC_SETUP.md), [docs/MODEL_SETUP.md](docs/MODEL_SETUP.md).

## Make it you
1. **Voice.** Click **Record my voice** (or run `scripts/make_studio_app.sh` to put a **Voice Studio** app on the Dock).
   Read the prompts, Keep the good takes, and aim for about 30 kept minutes. Then:
   ```sh
   uv run python scripts/make_voice_reference.py
   launchctl kickstart -k gui/$(id -u)/com.echopersona.voice
   ```
2. **Face.** Put a few clear, front-facing photos of yourself in `<dataset_root>/staging/face/photos-current/`, open
   **My face**, press Scan, mark your face **This is me** and star the best one. Set up LivePortrait once
   ([docs/PC_SETUP.md](docs/PC_SETUP.md)), then run `scripts/build_face.sh` (about 10 minutes) and reload Echo's window.
3. **Personality.** Answer the "about me" prompts in the Voice Studio (or the Collector app), then open **How I talk**,
   press Update, and approve the traits that sound like you.
4. **Memories.** Tell Echo "remember that …", or add them in **What I remember**.

### Collector app (Android, optional)
Download **EchoPersonaCollector.apk** from the [Releases](../../releases) page. With USB debugging on
([docs/ANDROID_SETUP.md](docs/ANDROID_SETUP.md)) and the phone plugged in:
```sh
brew install android-platform-tools
scripts/install_collector.sh ~/Downloads/EchoPersonaCollector.apk
```
Record sessions on the phone, then plug it in and press **Copy from phone** in the Voice Studio. Every file is checked
against its fingerprint before the phone may delete it. To build the app yourself: `cd collector-android && ./gradlew assembleDebug`.

## Opening web pages
"Hey Echo, open the Weather Channel", "go to apple dot com", "open Chrome to example dot org". Requests are recognised
before the language model sees them, so Echo only opens addresses that were actually said or saved as shortcuts; it
never invents one. Shortcuts live in `config/browser.json`. To open a site as a particular Google account, add
`?authuser=` to its address; for two people sharing one Mac, a plain "YouTube" can ask whose:
```json
{"say": "Sam's YouTube", "names": ["sam's youtube"], "url": "https://www.youtube.com/?authuser=sam@example.com"},
{"say": "Pat's YouTube", "names": ["pat's youtube"], "url": "https://www.youtube.com/?authuser=pat@example.com"},
{"say": "YouTube", "names": ["youtube"], "ask": "Sam's YouTube or Pat's?",
 "answers": {"sam's": "Sam's YouTube", "pat's": "Pat's YouTube"}}
```

## Privacy
- Every service listens on `127.0.0.1` only; nothing is reachable from the network. See [docs/SECURITY.md](docs/SECURITY.md).
- Original recordings and photos are stored read-only with SHA-256 checksums; consent is recorded before any recording.
- Run `scripts/backup_dataset.sh` after recording sessions; `scripts/offsite_backup.sh` makes an encrypted copy for
  another drive ([docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md)).
- **Optional Claude helper (off by default):** if you have Claude Code installed, the Status page can let it speed up a
  few slow background jobs (personality updates and summaries). Each job is switched on separately and sends only that
  job's text. Live conversation, audio, photos and your voice never leave the Mac.
- Only clone a voice or face with that person's clear consent. Echo asks for it before the first recording.

## Troubleshooting
See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). Quick checks:
```sh
launchctl list | grep echopersona          # the 4 background parts
curl -s 127.0.0.1:8765/health              # brain, memory, speech recognition
uv run pytest -q                           # automated tests
```

## License
MIT; see [LICENSE](LICENSE). Third-party models and libraries keep their own licenses:
[docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md) (for example, Parakeet is CC-BY-4.0 and needs attribution to NVIDIA,
and Llama 3.1 has its own community license).
