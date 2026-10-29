# Changelog

Echo Persona is alpha software: versions may change things without a migration path. Keep backups of your data.

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
