# Security and Privacy

Echo holds some of the most personal data there is: the owner's voice, face, words and memories. The design rule is
**everything stays on this Mac (and its drives) unless the owner explicitly decides otherwise.**

## What runs, and who can reach it
| Service | Started by | Listens on | Reachable from |
|---|---|---|---|
| Echo Persona Core (chat, memory, pages) | LaunchAgent `com.echopersona.core` | `127.0.0.1:8765` | this Mac only |
| The owner's voice (Qwen3-TTS) | LaunchAgent `com.echopersona.voice` | `127.0.0.1:8766` | this Mac only |
| Speech recognition (Parakeet) | LaunchAgent `com.echopersona.stt` | `127.0.0.1:8767` | this Mac only |
| Language model (Ollama) | LaunchAgent `com.echopersona.ollama` | `127.0.0.1:11434` | this Mac only |

Verified with `lsof -nP -iTCP -sTCP:LISTEN`: all four bind to 127.0.0.1. No router ports are opened and no
service accepts connections from the network. Check any time:
```
for p in 8765 8766 8767 11434; do lsof -nP -iTCP:$p -sTCP:LISTEN; done
```

## Network use
- **At runtime:** nothing, unless you switch on the optional Claude helper. Speech recognition, the language model, the
  voice and the face all run locally.
  Hugging Face downloads are disabled at runtime (`HF_HUB_OFFLINE=1`, telemetry off).
- **During setup only:** Homebrew, PyPI/uv, Hugging Face, Ollama's model registry, Google's Maven (Android build), GitHub
  (LivePortrait source). Each was a deliberate, approved download. Nothing personal is ever sent.
- **The phone app has no internet permission** (verified in the built APK: CAMERA, RECORD_AUDIO, POST_NOTIFICATIONS,
  RECEIVE_BOOT_COMPLETED). Recordings leave the phone only over a USB cable, via `adb`.
- **Optional Claude helper (off by default).** Your own Claude Code account can do three slow background jobs faster
  (`core/claude_helper.py`, switches on the Status page, stored in `config/claude.json`). Each job is switched on separately
  because each one sends text to Anthropic:
  - *Personality updates*: transcripts of the owner's own "about me" and free-talk recordings.
  - *Personality summary*: the traits the owner approved.
  - *Conversation notes*: conversations with the loved one. Leave this off unless the people you talk with agree.
  Never sent: audio, photos, voice samples, saved memories, live replies. Each call runs with no tools, files, MCP servers, settings or memory, from an empty folder, so Claude sees
  only that one prompt. When the helper is off, logged out, over its daily cap (60) or offline, the job runs locally as before.

## Where personal data lives
| Data | Location | Protected by |
|---|---|---|
| Original recordings, photos, phone sessions | external SSD `/Volumes/External storage/EchoPersona-Data/raw/` | read-only files, SHA-256 checksums |
| Derived data (transcripts, face frames, voice references, face atlases) | same SSD, `derived/` | versioned folders; reproducible from raw |
| Memories + conversation history | `~/EchoPersona/data/memory.sqlite` | macOS account login; Time Machine |
| Personality draft + approved profile | `~/EchoPersona/data/personality/` | macOS account login; Time Machine |
| Second copy of the dataset | `~/EchoPersona/data/dataset-backup/` | Time Machine (the SSD itself is excluded by Time Machine) |
| Consent records | SSD `consent/voice-consent.json`; phone `consent.json` (copied with each session) | read with the data |

**Encryption at rest (checked):** the Mac's internal drive is encrypted (FileVault on) and so is the Time
Machine drive. **The external SSD is not encrypted yet**, and it holds the original recordings and photos.

To encrypt it (Your decision; it needs a password you choose, so it isn't done automatically):
1. Make sure backups are current (Status page → Backups), and that you can write the password down somewhere safe.
2. In Finder, Control-click **External storage** in the sidebar → **Encrypt "External storage"…** → choose a password
   and a hint. It encrypts in the background (it can take hours for the whole 1.8 TB; the drive stays usable). Note this
   covers everything on that drive, not only Echo's folder.
3. After the next restart macOS asks for the password when the drive appears: tick **Remember this password in my
   keychain**, so it unlocks by itself at login. Until it's unlocked Echo uses the stand-in voice and simple face,
   then switches back automatically (the voice service retries every 30 s).
The Status page shows "SSD encrypted: yes" once it's done.

**Off-site copy:** `scripts/offsite_backup.sh /Volumes/<drive>` writes an AES-256 encrypted disk image with everything
that matters (see BACKUP_AND_RECOVERY.md). Its password is chosen when it's first made and is never stored.

**Logs** (`~/EchoPersona/logs/`) never contain conversation text or transcripts, only events like "turn ok" or "stt ok".
`ECHO_LOG_TRANSCRIPTS=1` enables transcript logging for supervised testing only; it is off by default.

**Git:** `.gitignore` excludes `data/`, models, audio/video/images, databases and keys. The repository contains code and
documentation only.

## Consent and control
- Recording happens only when someone presses Record (Voice Studio, phone app). A red indicator shows whenever a
  microphone or camera is on. "Hey Echo" mode keeps the mic open but discards anything without the wake phrase
  (never shown, stored or logged).
- Nothing becomes a *fact* automatically: only "remember that…", the memory panel, or a fact the owner saved in How I talk.
  Echo does keep short **conversation notes** (1–3 sentences per ~16 messages that have scrolled out of view), written
  locally while nobody is talking, marked "inferred", and listed in What I remember where they can be edited or forgotten.
- Personality traits are used only after the owner approves them, and each is backed by a quote of their own words.

## Deleting things
- A memory: Echo window → What I remember → Forget.
- Conversation history: What I remember → Clear conversation (this removes all of it).
- A recording: originals are read-only on purpose. To delete one deliberately, `chmod u+w` the file and remove it plus
  its `.meta.json`; derived files that point to it (same take name under `derived/`) should be removed too.
- **Deleting a file does not guarantee it is unrecoverable** on SSDs, in Time Machine snapshots, or in the backup copy.

## Third-party model licences
See THIRD_PARTY_LICENSES.md. Note the InsightFace detection models bundled with LivePortrait are **non-commercial only**
(fine for this personal use).
