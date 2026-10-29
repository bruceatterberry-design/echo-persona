# Echo Architecture (as built, 2026-10-27)

Local-first digital self of its owner, made for someone they love. One Apple Silicon Mac (16 GB+), an external SSD for the dataset,
an Android phone for collection. No cloud services at runtime.

```
             Microphone (USB)                                         Speakers
                   │                                                     ▲
   ┌───────────────▼──────────────────── Echo window (Chrome app, 127.0.0.1:8765) ─────────────────┐
   │ voice activity detection (audio thread, 0.6 s pre-roll, adaptive noise floor)                       │
   │ "Hey Echo" → conversation mode (until "that's all" / 20 s quiet) · push-to-talk · barge-in       │
   │ face: pre-rendered frames switched live (head pose × expression × mouth × eyes) + the owner's idle video │
   │ mouth = sound class (F1/F2 analysis of the voice as it plays) × jaw height (loudness)               │
   └───────┬──────────────────────────────────────────▲───────────────────────────────▲─────────────────┘
           │ WAV (/api/stt)            text deltas    │ WebSocket /ws      PCM stream  │ /api/tts/stream
   ┌───────▼──────────────────────────────────────────┴───────────────────────────────┴─────────────────┐
   │ Echo Persona Core (FastAPI, Python 3.12, LaunchAgent com.echopersona.core)                                   │
   │  speech recognition: mlx-whisper large-v3-turbo (wake-phrase check, transcripts never logged)       │
   │  conversation: base personality + the owner-approved traits + 2 of their real sentences + memories       │
   │  streaming reply parser: expression tag, stage directions → avatar expression (never spoken)       │
   │  memory: SQLite (facts/preferences, confirmed vs inferred, FTS search) + conversation log          │
   │  pages: Avatar Collector, Face Library, How I talk, memory panel; phone copy + processing jobs         │
   └──────┬────────────────────────────┬──────────────────────────────┬────────────────────────────────┘
          │ /api/chat (stream)          │ /stream (PCM)                │ adb over USB
   ┌──────▼─────────┐         ┌─────────▼──────────────┐       ┌───────▼──────────────────────────────┐
   │ Ollama          │         │ Voice service           │       │ Echo Persona Collector (Android, no internet)│
   │ llama3.1:8b     │         │ Qwen3-TTS 0.6B (MLX)    │       │ guided sessions: face video, photos,    │
   │ 127.0.0.1:11434 │         │ ref: the owner's 6.2 s clip │       │ read-aloud WAV, own-words answers;      │
   │ keep-alive 24 h │         │ 127.0.0.1:8766          │       │ checksums, receipts, reminders          │
   └─────────────────┘         └─────────────────────────┘       └─────────────────────────────────────────┘

 Offline tools (run when needed, not during conversation):
   face/facefind (Apple Vision) · face/render_face.py + LivePortrait (atlas frames) · face/motion_post.py (idle loop)
   processing/ingest.py (phone sessions → voice QC, face frames, personality examples) · personality/build.py
```

## Key decisions
| Decision | Why |
|---|---|
| Own small core instead of a large assistant framework | Fewer moving parts and dependencies; every piece replaceable behind an interface (`LLMBackend`, `STTBackend`, voice HTTP service, face frames) |
| Pre-rendered face + live frame switching, not live neural animation | LivePortrait runs ~30× slower than real time on the M4; switching frames costs nothing and never blocks conversation |
| Separate Python environments (core, voice, LivePortrait) talking over localhost / files | Their dependencies conflict; a crash in one doesn't take down the others |
| Zero-shot voice cloning from one reference, no fine-tuning | Good likeness with 30 min of data; easy to change; you can compare references by ear |
| Personality from approved, quoted traits (retrieval + instructions), no fine-tuning | Can't invent a personality; the owner controls every trait; works with ~2,000 words |
| USB instead of Wi-Fi for the phone | No network listener at all |
| Streaming end to end (LLM tokens → phrases → voice PCM) | First sound 3.5–5 s after a question instead of 9–12 s |

## Main code
| Path | Role |
|---|---|
| `core/server.py` | HTTP + WebSocket server, all APIs |
| `core/llm.py`, `core/schema.py` | LLM backends; reply validation, expression tags, stage-direction filter |
| `memory/store.py` | durable memory + conversation log |
| `voice/stt.py`, `voice/wake.py`, `voice/studio.py` | speech recognition, wake/end phrases, recording studio |
| `tts/service.py` | The owner's voice (separate env) |
| `face/library.py`, `face/facefind.swift`, `face/render_face.py`, `face/motion_post.py` | face references, detection, rendering |
| `transfer/phone_sync.py`, `processing/ingest.py` | USB copy with checksums; processing of phone sessions |
| `personality/build.py` | grounded personality traits |
| `renderer/*.html` | the window and the review pages (plain HTML/JS, no build step) |
| `collector-android/` | Kotlin + Compose phone app |
| `scripts/` | autostart, backup, icon, installers |

Data layout: DATASET_FORMAT.md. Security: SECURITY.md. Measurements: BENCHMARKS.md.
