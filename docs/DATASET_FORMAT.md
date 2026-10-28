# Dataset Format (voice, v1)

Root: `/Volumes/External storage/EchoPersona-Data` (config `dataset_root`). Never committed to git.

```
consent/voice-consent.json                      the owner's authorization: name, statement, time, scope, local-only
raw/voice/<YYYYMMDD-HHMMSS>/take-NNNN.wav       ORIGINAL take, 16-bit mono PCM at the mic's rate (48 kHz), read-only
raw/voice/<session>/take-NNNN.meta.json         written once: prompt id/text/category, read-aloud flag, time,
                                                format, bytes, sha256, capture settings (all browser DSP off), consent ref
derived/voice/qc-v1/<session>/take-NNNN.json    quality report (duration, peak/speech/noise dBFS, SNR, clipping %,
                                                verdict good/warn/fail, issues), Whisper transcript, read_match 0–1,
                                                status pending/kept/rejected, source_sha256 → links back to the original
```

Rules
- Originals are created with exclusive-create (never overwritten) and set read-only.
- Rejected takes are kept on disk and marked `rejected`; nothing is auto-deleted.
- Derived data is versioned (`qc-v1`). A newer pipeline writes `qc-v2` alongside it.
- Prompts live in `voice/prompts.yaml`; ids look like `calm-03`. `custom` = free talk.

# Phone sessions (Echo Persona Collector), v1
```
raw/phone/<YYYYMMDD-HHMMSS>/take-NNN.(wav|mp4|jpg)   originals from the phone, read-only (folder too)
raw/phone/<id>/take-NNN.json                        per-take metadata from the phone: task id/kind/category, prompt text,
                                                    expression label (the instructed task, not an emotion), duration,
                                                    bytes, sha256, status kept/rejected/unreviewed, capture info
raw/phone/<id>/manifest.json                        all takes of the session
raw/phone/<id>/receipt.json                         Mac's verification: {file: sha256}; also pushed back to the phone
derived/phone/ingest-v1/<id>/done.json              processing report (delete it to reprocess the session)
derived/voice/qc-v1/phone-<id>/take-NNN.json        voice QC + transcript (same format as studio takes)
derived/face/phone-frames-v1/<id>/<take>/            best frames + frames.json (quality, time, transcript)
derived/personality/examples-v1.jsonl               own-words answers: question, transcript, source, reviewed flag
staging/face/photos-current/phone/                  phone photos waiting for review in the Face Library
```

# Face, v1
```
staging/face/photos-current|photos-younger/...      dropped-in photos (never modified)
raw/face/<sha16>.<ext> (+ .meta.json)               originals of approved photos, read-only, checksum-verified
derived/face/crops-v1/                              512 px face crops (Apple Vision)
derived/face/index-v1.json                          catalog: photos, faces, quality, review status, ★, age labels
derived/face/atlas-v5/                              415 rendered frames (pose/expression/mouth/eye) + atlas.json; source-working.jpg
derived/face/motion-v1/idle-calm.mp4 (+ motion.json) idle loop from the owner's own video; driving/ = the trimmed source clip
derived/face/atlas-v1…v4, atlas-calib-v1            earlier renders and calibration sheets (kept for comparison)
```

# Voice references
`derived/voice/refs-v1/ref-<name>.wav` + `refs.json` (each entry names its source take and sha256). Active one: `config/voice.json`.

# Personality (internal drive, not the SSD)
`~/EchoPersona/data/personality/draft.json` (traits with quotes and status, facts, style stats), `profile.json` (only what the owner
approved; used by every reply), `evals.jsonl` (Try-it ratings).

# Memory (internal drive)
`~/EchoPersona/data/memory.sqlite`: tables `memories` (kind, content, confirmed/inferred, confidence, source, times) with a
full-text index, `turns` (conversation log) and `meta` (`summarized_upto`: last turn covered by a note). Conversation
notes are memories with kind `summary`, status `inferred`, source `auto:conversation-summary`. Snapshots: `data/backups/`.
