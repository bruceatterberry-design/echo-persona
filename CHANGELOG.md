# Changelog

Echo Persona is alpha software: versions may change things without a migration path. Keep backups of your data.

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
