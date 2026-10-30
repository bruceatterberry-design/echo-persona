# Echo Persona

A private, local "digital self" for your Mac: a talking companion that speaks in **your voice**, shows **your face**,
remembers what you tell it, and talks the way you do. It's made to be left for someone you love.

Everything runs on your own Mac. Recordings, photos, memories and conversations never leave it, and nothing is
uploaded or sent to any outside service. (The one optional exception, switched off by default, is described under Privacy.)

- **Talks and listens:** say "Hey Echo, …" or use push-to-talk. Local speech recognition (Parakeet / Whisper) and a
  local language model (Llama 3.1 8B via Ollama). Replies stream, so it starts speaking within a few seconds.
- **Your voice:** record about 30 minutes in the built-in **Avatar Collector**; Echo clones your voice from your best takes
  (Qwen3-TTS, zero-shot, no training run needed).
- **Your face:** add a few photos; Echo renders a talking, blinking face from one of them (LivePortrait, offline).
  The mouth follows the voice as it plays.
- **Remembers:** say "remember that …" or use the memory panel. Nothing is saved without being asked.
- **Talks like you:** the "How I talk" page proposes personality traits from your own recorded answers, each backed by a
  quote; only the ones you approve are used.
- **Opens web pages:** "Hey Echo, open the Weather Channel" or "go to weather dot com".
- **Android Collector app (optional):** guided daily sessions that record your voice and face video on your phone, and
  copy to the Mac over a USB cable only (the app has no internet permission).

> **⚠️ Alpha software.** Echo Persona is early, experimental software (version 0.3.0). Expect rough edges, breaking
> changes between versions, and features that have only been tested on one Mac. Keep backups of your recordings
> (`scripts/backup_dataset.sh`) and don't rely on it for anything important yet.

## What's new in 0.3.0
- **Life memories.** Memories can carry when they happened (childhood, teens, young adult, adult, recent; year; your
  own words), who was there and where. Repeats are merged instead of piling up, and memories that may contradict each
  other are set aside for you to decide (Keep 1 / Keep 2 / Both are true); nothing is overwritten automatically.
- **Search by meaning** with a small local model (`ollama pull nomic-embed-text`, 274 MB): a question finds related
  memories and past conversations even when they share no words. Falls back to keyword search without it.
- **"Tell me a memory" on the phone** (Collector 0.3.0): audio-only stories, 3 questions per session from different
  times of life. After "Copy from phone", Echo transcribes them and suggests memories, each with your exact words as
  evidence. Review them on the new **My memories** page (fix, keep or reject, replay the recording, life-story view).
- **Echo asks you about your life**, now and then, but only when it recognises *your* voice (or when you say "ask me
  something"). Your answers become suggestions on My memories.
- **Who's talking:** local speaker recognition (sherpa-onnx + WeSpeaker, 26.5 MB) tells the owner's voice from the
  loved one's; the loved one adds their voice on the **Who's talking** page with their consent. See docs/SPEAKERS.md.
- **Personality versions and checks:** every change to "How I talk" can be compared and undone, and a small set of
  checks (honesty, identity, no pet names for the owner, no invented quotes, short replies) runs through the real prompt.

## What's new in 0.2.0
- **Five new expressions from your own recordings:** joy, surprised, sad, angry and bewildered. Record them with the
  Collector app's new **Record my expressions** session (about 3 minutes). Echo measures how your face moves from
  relaxed to each expression and carries that movement onto your avatar's photo, so everything else about the photo stays
  the same. You pick the strength and how much of the mouth to keep (`config/face_expressions.json`).
- **The face follows the voice:** each spoken phrase carries its own expression, and the face changes as you *hear* it.
- **Facial cues are never read aloud:** when the language model writes `(smiles)`, `(laughs)`, `(sad)`, `[nods]` or
  `*chuckles*`, the words are removed and the face shows the expression instead. Normal parentheses are kept.
- **Blink and smile on request:** "Blink for me" and "smile for me" work even if the model forgets the cue.
- **Echo knows it has a face:** the personality prompt describes the avatar and the cue words, so it no longer claims
  to be "text only".
- **Smoother voice:** a small playback buffer removes most of the choppiness when the Mac is busy (65 → 5 gaps in a
  test of 8 replies while the language model was still writing).
- **Voice Studio is now Avatar Collector:** voice recording, copying from the phone, and expression previews in one
  place. "Put on the avatar's face" builds a new face version; nothing changes until you click it.
- Fixes: pages are never served from a stale browser cache; a half-built face is never loaded.

Full list: [CHANGELOG.md](CHANGELOG.md).

> Echo Persona is a personal project, not a product. It is honest about being a digital self built from what you chose to
> share, not a living person.

## Requirements
- A Mac with Apple Silicon (M1 or later) and **16 GB of memory or more**. Tested on a Mac mini M4 with 16 GB.
  Echo currently runs on **macOS only**; see *GPU and memory* below and *Windows* (planned).
- About 20 GB free for models, plus room for your recordings. An external SSD for the data is recommended.
- Google Chrome (Echo's window is a Chrome app window).
- [Homebrew](https://brew.sh).
- Optional: an Android phone (Android 8+) for the Collector app.

### GPU and memory
On a Mac, the GPU shares the computer's memory ("unified memory"), so all of Echo's models have to fit in RAM together
with macOS and Chrome. Measured on a Mac mini M4 with 16 GB:

| Part | Model | Memory while loaded | Speed |
|---|---|---|---|
| Thinking | Llama 3.1 8B (4-bit, Ollama) | ≈ 4.9 GB | ≈ 19 tokens per second |
| Your voice | Qwen3-TTS 0.6B (MLX) | ≈ 2.6 GB | ≈ 1.3× faster than real time on its own |
| Listening | Parakeet TDT 0.6B v2 (MLX) | ≈ 2.6 GB | ≈ 0.13 s per question |
| Face builds (offline, not while chatting) | LivePortrait (PyTorch MPS) | peak ≈ 5 GB | ≈ 1.3 s per frame, ≈ 10 min per face |

Limitations to know about:
- **16 GB is the minimum, and it is tight.** With everything loaded, about 10 GB is in use before macOS and Chrome. If
  the Mac starts swapping, replies slow down and the voice can stutter. Close other heavy apps, and don't build a new
  face while you're talking to Echo.
- **The language model and the voice share one GPU.** While the model is still writing an answer, voice generation
  slows to below real time, so Echo waits a moment (0.7–2 s) to buffer speech before talking. More GPU cores
  (M-Pro/Max) and **24 GB or more** give noticeably smoother speech.
- **8 GB Macs are not supported**: the models alone don't fit.
- Disk: about 20 GB for models, plus about 30 MB per face version and whatever your recordings take.

### Windows (planned, not yet available)
A future version may support Windows 10/11 with an **NVIDIA GPU** (CUDA). The language model would run through Ollama
and the voice, listening and face models through their PyTorch/CUDA versions instead of Apple's MLX. Expected needs,
estimated from the Mac measurements and **not yet tested**:
- **Minimum:** NVIDIA RTX GPU with **8 GB of video memory** (e.g. RTX 3060 8 GB, 4060) and 16 GB of system RAM. With
  8 GB, the models can't all stay on the GPU at once, so expect slower replies.
- **Recommended:** **12–16 GB of video memory** (e.g. RTX 3060 12 GB, 4070, 4080) so the language model, voice and
  listening models stay loaded together.
- AMD and Intel GPUs are not planned for now.

## Install
```sh
git clone https://github.com/bruceatterberry-design/echo-persona.git ~/EchoPersona
cd ~/EchoPersona
brew install uv ffmpeg ollama
uv sync && (cd tts && uv sync)
brew services start ollama   # or: ollama serve
ollama pull llama3.1:8b
ollama pull nomic-embed-text   # memory search by meaning (274 MB)
mkdir -p models/speaker && curl -L -o models/speaker/wespeaker_en_voxceleb_resnet34.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx   # who's talking (26.5 MB)
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
1. **Voice.** Click **Avatar Collector** (or run `scripts/make_studio_app.sh` to put an **Avatar Collector** app on the Dock).
   Read the prompts, Keep the good takes, and aim for about 30 kept minutes. Then:
   ```sh
   uv run python scripts/make_voice_reference.py
   launchctl kickstart -k gui/$(id -u)/com.echopersona.voice
   ```
2. **Face.** Put a few clear, front-facing photos of yourself in `<dataset_root>/staging/face/photos-current/`, open
   **My face**, press Scan, mark your face **This is me** and star the best one. Set up LivePortrait once
   ([docs/PC_SETUP.md](docs/PC_SETUP.md)), then run `scripts/build_face.sh` (about 10 minutes) and reload Echo's window.
3. **Personality.** Answer the "about me" prompts in the Avatar Collector (or the Collector app), then open **How I talk**,
   press Update, and approve the traits that sound like you.
4. **Expressions (new in 0.2.0).** On the phone, run **Record my expressions** (and at least one normal session, which
   takes the relaxed "straight on" photo used as the starting point). After **Copy from phone**, Avatar Collector makes a
   preview of each feeling on your face. Check it, adjust `config/face_expressions.json` if needed (photo or video,
   strength, how much of the mouth to keep), then click **Put on the avatar's face** (about 10 minutes) and reload Echo's window.
5. **Memories.** Tell Echo "remember that …", or add them in **What I remember**.

### Collector app (Android, optional)
Download **EchoPersonaCollector.apk** from the [Releases](../../releases) page. With USB debugging on
([docs/ANDROID_SETUP.md](docs/ANDROID_SETUP.md)) and the phone plugged in:
```sh
brew install android-platform-tools
scripts/install_collector.sh ~/Downloads/EchoPersonaCollector.apk
```
Record sessions on the phone, then plug it in and press **Copy from phone** in the Avatar Collector. Every file is checked
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
