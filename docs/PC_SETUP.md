# Mac Setup

Built and tested on a Mac mini M4 with 16 GB. Apple Silicon only (MLX; no NVIDIA/CUDA). The examples assume the project is at `~/EchoPersona`.

## Tools (Homebrew, no admin password needed)
```
brew install uv node ffmpeg ollama openjdk@17 gradle
brew install --cask android-commandlinetools        # only needed to rebuild the phone app
```
Versions used: Ollama 0.40.11, uv 0.13.6, Node 26.12.1, ffmpeg 9.0.2, OpenJDK 17.0.21, Gradle 9.8.2.
Python 3.12 (core, voice) and 3.11 (LivePortrait) are managed by uv; nothing touches the system Python.

## Environments
| Folder | What | Set up with |
|---|---|---|
| `~/EchoPersona` | Echo Persona Core (FastAPI, memory, speech recognition, pages) | `uv sync` |
| `~/EchoPersona/tts` | The owner's voice service (mlx-audio 0.6.2) | `cd tts && uv sync` |
| `~/EchoPersona/animate` | LivePortrait (face rendering, offline only) | git clone KlingTeam/LivePortrait @ 9b294b3; `uv venv --python 3.11 .venv`; install torch 2.3.0, torchvision 0.18.0, onnxruntime 1.19.2, requests 2.32.3 and `requirements_base.txt` minus gradio |
| `~/EchoPersona/collector-android` | Phone app | `./gradlew assembleDebug` with `JAVA_HOME=/opt/homebrew/opt/openjdk@17` |

Models: see MODEL_SETUP.md. The face finder is built with `scripts/build_face_tools.sh` (Swift, Apple Vision).

## Running
```
~/EchoPersona/scripts/install_autostart.sh     # LaunchAgents + /Applications/Echo Persona.app + login item + face icon
```
This creates 4 background services (`com.echopersona.ollama`, `.voice`, `.stt`, `.core`). After that Echo starts at login, restarts itself if a part crashes, and opens on the left half of the screen
(Chrome app window, own profile `data/chrome-echo`). Remove with `scripts/uninstall_autostart.sh` (data untouched).

Useful commands:
```
launchctl list | grep echo                                   # are the 4 parts running?
launchctl kickstart -k gui/$(id -u)/com.echopersona.core            # restart one part (.core / .voice / .stt / .ollama)
open "/Applications/Echo Persona.app"                                # open the window
curl -s 127.0.0.1:8765/health                                   # status of brain, memory, speech recognition
curl -s 127.0.0.1:8766/health                                   # status of the owner's voice
curl -s 127.0.0.1:8767/health                                   # status of speech recognition (Parakeet)
cd ~/EchoPersona && uv run pytest -q                                # automated tests
```

## Re-create after a restore (new Mac or reinstalled macOS)
1. Restore the user account from Time Machine so `~/EchoPersona` comes back (code, data, models, venvs).
2. Install the tools above (Homebrew installs are not in your home folder).
3. `cd ~/EchoPersona && uv sync && (cd tts && uv sync)`; if `~/EchoPersona/animate/.venv` is broken, recreate it as in the table.
4. `ollama serve` once and `ollama pull llama3.1:8b` if `~/.ollama/models` didn't come back.
5. `scripts/build_face_tools.sh`, then `scripts/install_autostart.sh`.
6. Plug in the external SSD (or restore it, see BACKUP_AND_RECOVERY.md) and open Echo.
7. In the new Echo window click **Allow** for the microphone.

## Settings worth knowing
- `config/voice.json`: which voice reference to use (set by `scripts/make_voice_reference.py`), and which speech
  recognizer: `"stt": "parakeet"` (default now) or `"whisper"`; restart `com.echopersona.core` after changing it.
- `config/personality.example.yaml`: Echo's base personality. Copy to `config/personality.yaml` to customise.
- `core/config.py` defaults: model `llama3.1:8b`, keep-alive 24 h, dataset at `/Volumes/External storage/EchoPersona-Data`.
- Optional `config/echo.yaml` overrides any of those (see `PersonaConfig`).
