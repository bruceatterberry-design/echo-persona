# Changelog

Echo Persona is alpha software: versions may change things without a migration path. Keep backups of your data.

## 0.4.0-alpha (2026-10-31): reads the web, listens to music, learns from 👍/👎, knows your voice in the room

### Smoother voice and faster replies on 16 GB Macs (fix: choppy voice, slow answers)
- **Cause:** Echo's voice, speech recognition and language model share one 16 GB memory and one graphics chip. The voice
  ran at 0.96× real time (slower than speech), so it stuttered whenever the model was thinking; the Mac was also
  swapping 3.1 GB to disk, which slowed every answer (15–24 s to the first word).
- **Precision now follows the Mac's memory** (checked every time Echo starts, tts/hw.py):
  up to 16 GB → 8-bit voice and half-precision speech recognition; 24 GB or more → 16-bit voice and full-precision
  speech recognition. Override with ECHO_TTS_BITS=16|8 and ECHO_STT_DTYPE=f32|bf16.
- **Measured on a Mac mini M4, 16 GB:** voice 0.96× → 1.61× real time and 2.65 → 1.86 GB; speech recognition
  2.51 → 1.28 GB with the same words (12 test recordings); swap 3.1 → 0.9 GB; first spoken word 15–24 s → 3.6–7.8 s
  on web questions; while the model is busy, voice gaps 3 → 2 and silence 4.9 → 2.0 s, first sound 0.5–2.1 s.
- **The installer checks this too** and says which versions this Mac will use.

### Who's talking (fix: Echo called the owner by the loved one's name)
- **Cause:** the owner's voice profile was built from studio takes (mic close to the mouth). Through the room microphone it
  scored only 0.30–0.44 (0.72 needed), so the owner was "unknown", and Echo guessed the person it talks with most.
- **Echo now listens for the owner's voice now and then and learns the room:** a phrase that is confidently the owner adds a
  room voice print (at most once every 6 hours); an unclear voice that sounds most like the owner makes Echo ask
  "Is that you, <name>?" (at most every 30 minutes), and "yes" adds that phrase's print. Profiles keep two centroids
  (recordings, room mic); the room one counts after 3 prints; newest 30 kept. Only voice prints are stored, never audio.
  Only the owner's profile grows this way; the loved one's only with their own consent.
- **No guessing names:** when the voice isn't recognised, Echo doesn't call anyone by name.
- **"This is <name>"** said out loud (names in `config/echo.yaml`: owner_name, loved_one_name): that phrase (about a second of voice from the room mic) is added
  to that person's room voice print, and Echo knows who's talking. Only for people who already have a profile (made
  with their consent); skipped if the voice clearly belongs to the other person (a joke); typed names add nothing.

### Music
- **"Open YouTube and play some Steely Dan"**, "play Hotel California on YouTube", "would you open up a Steely Dan song
  and listen to it with me?", "can we listen to some blues": the top YouTube video plays in Echo's own music window.
- **The microphone is off while music plays** (Echo hears the song from YouTube itself) and comes back in its previous
  mode when the music window closes: Echo closes it when the song ends; if you close it early he notices at once.
- **Listening along:** Echo analyses the same song (tempo, energy, sound, the words he can make out), stays quiet while
  it plays, then says something like "That was … What did you think of it?"
- **"Listen to this <YouTube link>"**: Echo learns a song as knowledge. Only approved genres/artists
  (`config/music.json`, yours to edit); otherwise nothing is downloaded and he offers "add <genre> to my music list".
  Audio is deleted after analysis. Key is a "best guess"; mood comes from tempo/energy, not major/minor.

### Web (ideas adapted from JARVIS, MIT)
- **Safe page reader:** public internet only (this Mac, home network, Tailscale and cloud-metadata addresses refused,
  every redirect re-checked), size/type limits, main text without menus/footers/cookie banners.
- **Echo reads pages himself** with tools on the local model (Ollama tool calling): only addresses you gave him; page
  text treated as untrusted. **Research** ("research…", "follow the links"): up to 5 linked pages, exact links only.
  **Sources** listed under the reply. Tool settings in config/tools.json; every use logged without content.
- **Web search:** "look up …", "search the web for …": Echo searches DuckDuckGo (no extra software), reads the best
  result before answering, and cites it. Only the search words go to DuckDuckGo.
- **Pages that need JavaScript:** when the plain reader finds almost nothing, Echo opens the page in an invisible Google
  Chrome (Playwright; no extra browser download) with an empty profile: no logins or cookies, no downloads, no pop-ups,
  and every request the page makes must go to the public internet (tested: a hostile page could not reach Echo's own
  services, the home router or a cloud-metadata address). Normal pages still use the fast reader.

### Training
- **"use Claude to evaluate Echo"**: 👍/👎 on every reply (and things Echo says on his own). **"Train Echo"**: Claude
  (your own Claude Code account, only after "are you sure?") explains each 👎 and offers fixes; you pick, Claude checks,
  you say yes; a lesson is added to Echo's instructions and the same words are replayed until 👍. Then testing turns off
  and Claude is disconnected. Echo checks he's signed in to Claude and opens the sign-in when needed. Reminder line
  under the face.

### Honesty fixes
- Echo no longer says he's opening or playing something that didn't happen (a note on the message when a request
  isn't recognised), and no longer copies old "Tell me any memory…" questions onto unrelated replies. No life questions
  right after a command or while music plays.

### Install and requirements
- **Required:** Apple Silicon (M1 or later) and 16 GB of memory or more; `scripts/install_autostart.sh` checks this and
  prints which model versions it will use.
- New settings: `owner_name` in `config/echo.yaml`; `config/tools.json` (web tools), `config/music.json` (approved music).
- New packages: faiss-cpu, yt-dlp, librosa, playwright (drives your installed Google Chrome; no extra browser download).
- Report with before/after graphs: [docs/report](docs/report/README.md).

### Coming soon
- Installers: Mac (.dmg), Linux (.deb) and Windows (.exe); NVIDIA and AMD graphics builds.

## 0.3.0 (2026-10-30)

### Memory
- Life memories: new kind `memory` with optional `period`, `when_text`, `year`, `people`, `place`, `told_by`. Older
  databases gain the columns on first open (a backup copy is made first).
- Duplicate merging on save and import (word overlap ≥ 0.85); a repeat can upgrade "inferred" to "confirmed", never down.
- Possible contradictions (`memory/conflicts.py`): related memories are judged by the local model in the background and
  set aside for review; `/api/memory/conflicts`. Nothing is resolved automatically.
- Search by meaning (`memory/embed.py`, nomic-embed-text via Ollama): vectors made in the background; up to 2 related
  notes per message (cosine ≥ 0.62). Keyword search if the model is missing.
- `scripts/offsite_backup.sh` fallback copies the memory database with `sqlite3 .backup` (WAL-safe).

### Learning
- Collector 0.3.0: "Tell me a memory" (audio-only WAV, no time limit, 3 questions from different life periods,
  `assets/memories.json`), `life_period` in take metadata.
- Long recordings transcribed in ~60 s pieces cut at quiet moments; transcripts in `derived/memories/transcripts-v1/`.
- Suggested memories (`memory/candidates.py`): local model writes notes with exact quotes; notes whose quote isn't in the
  transcript are dropped, years kept only if said. Review page `renderer/memories.html`.
- Echo asks the owner about their life (`memory/curiosity.py`): owner's voice only, at most every 30 min, or on "ask me
  something"; answers turned into suggestions at the next quiet moment.

### Who's talking
- `voice/speaker.py` (sherpa-onnx 2.0.1 + WeSpeaker ResNet34), `voice/enroll.py`, `renderer/voices.html` with consent and
  "Forget this voice". `/api/stt` returns `speaker`; the model is told who is speaking with that message only.

### Personality
- `personality/versions.py`: versions of profile + review decisions + base prompt; compare and restore (undoable).
- `personality/evals.py`: checks run through the real prompt path; Checks and Versions tabs on "How I talk".
- Prompt template: no pet names for the owner; never put words in anyone's mouth.

## 0.2.0 (2026-10-29)

### Face and expressions
- **Five new expressions from your own recordings: joy, surprised, sad, angry, bewildered.** `face/expression_transfer.py`
  measures how your face moves from your relaxed photo to each expression (LivePortrait expression keypoints) and adds
  that movement to the avatar's current photo. Choices per feeling (expression photo or strongest video frame, strength,
  share of the mouth movement kept at rest and while talking) are in `config/face_expressions.json`.
- New face versions are built next to the old ones (`atlas-vN`, built in a `-partial` folder and renamed when done); the
  newest finished one is used automatically, and older ones are kept.
- **The face follows the voice.** Each spoken phrase carries its own expression and blinks, applied when the phrase is heard.
- **Facial cues are never spoken.** `(smiles)`, `(laughs)`, `(sad)`, `(leaning in close)`, `[nods]`, `*chuckles*` are removed
  from the shown and spoken text and drive the face instead. Ordinary parentheses ("the blue one", "about 2 hours") are kept.
  Removing a cue no longer leaves a stray space before punctuation.
- **Blink and smile on request:** asking to blink or smile triggers it even if the model forgets the cue; `(blinks)` / `(winks)` blink.
- **The prompt knows about the face:** `config/personality.example.yaml` describes the avatar, the 10 expression tags and the cue words.
- Expressions: `neutral, smiling, thoughtful, attentive, concerned` + `joy, surprised, sad, angry, bewildered`.

### Voice
- **Jitter buffer:** after any silence, playback waits until 0.7 s of speech is ready, more after a gap (up to 2 s),
  less again after smooth replies; 5 ms fade-in. Busy-Mac test, 8 replies: 65 gaps → 5. `scripts/bench_voice_gaps.py`.

### Avatar Collector (was "Voice Studio")
- Renamed app, page and voice shortcut ("Hey Echo, open the avatar collector").
- After **Copy from phone**, expression sessions get a preview sheet automatically; **Put on the avatar's face** builds
  the next face version with your saved choices. Nothing changes until you click it.

### Collector app 0.2.0 (Android)
- New **Record my expressions** session: for joy, surprised, sad, angry and bewildered, a photo holding the expression and
  a short video from relaxed → full → relaxed. Permissions unchanged (camera, microphone, notifications; no internet).

### Other
- Pages send `Cache-Control: no-cache` and face frames are versioned, so a reload never runs stale code.
- `POST /api/say` (this Mac only) and `scripts/say_test.sh "text with (cues)"` for testing the face and voice together.
- README: alpha notice, GPU and memory limits, planned Windows support.

## 0.1.0 (2026-10-27)
First public version.
