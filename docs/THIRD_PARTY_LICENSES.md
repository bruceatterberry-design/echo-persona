# Third-Party Components

| Component | Version / pin | Use in Echo | License | Notes |
|---|---|---|---|---|
| Ollama + Llama 3.1 8B | 0.40.11 / llama3.1:8b | conversation | MIT (Ollama); Llama 3.1 Community License (model) | local only |
| mlx-whisper + whisper-large-v3-turbo | 0.4.2 | speech recognition | MIT (code); MIT (OpenAI Whisper weights) | |
| NVIDIA Parakeet TDT 0.6B v2 (mlx-community) | — | speech recognition (chosen 2026-10-27) | CC-BY-4.0 | attribution: NVIDIA |
| mlx-audio | 0.6.2 | voice-cloning runtime (separate env tts/) | MIT | |
| Qwen3-TTS 12Hz 0.6B Base (mlx-community bf16) | — | The owner's voice (chosen) | Apache-2.0 | |
| Chatterbox multilingual v3 + S3TokenizerV2 | — | evaluated, not used | MIT; tokenizer card unlisted (CosyVoice upstream Apache-2.0) | can be deleted to save 3.2 GB |
| LivePortrait (KlingTeam) | git 9b294b3 (2026-06-02), separate env animate/ | face animation (offline pre-render) | MIT (code + weights) | |
| InsightFace buffalo_l detection models (bundled with LivePortrait) | — | face detection inside LivePortrait | **non-commercial research only** | fine for personal use; replace before any commercial use |
| PyTorch | 2.3.0 (animate env) | LivePortrait runtime | BSD-3 | |
| Apple Vision framework | macOS 27 | face finding in the face library | Apple SDK | on-device |
| FastAPI, Uvicorn, Pydantic, httpx, PyYAML, NumPy, Pillow | see uv.lock | core service | MIT / BSD / HPND | |

## sherpa-onnx 2.0.1 (speaker recognition, 2026-10-30)
Apache-2.0, https://github.com/k2-fsa/sherpa-onnx. Model: WeSpeaker ResNet34 (VoxCeleb), downloaded from the sherpa-onnx
releases; check the WeSpeaker project's model licence before redistributing it.
- **yt-dlp** 2026.10.26 (Unlicense, public domain): fetches audio of approved music videos from YouTube for Echo to analyse; audio deleted after. Added 2026-10-31 with the owner's OK.
- **librosa** 1.0.0 (ISC): music analysis (tempo, key, loudness/energy). Added 2026-10-31 with the owner's OK. Pulls scikit-learn (BSD-3), soundfile (BSD-3), soxr (LGPL-2.1), pooch (BSD-3).
- JARVIS (github.com/bertrandmbanwi/Jarvis, MIT): ideas adapted (not copied wholesale) in core/web.py, core/tools.py, core/permissions.py.
- **Playwright** 1.64.1 for Python (Apache-2.0, Microsoft): drives the Google Chrome already installed (channel "chrome") for pages that need JavaScript (core/jsbrowser.py). No Chromium download. Added 2026-10-31 with the owner's OK. Pulls greenlet (MIT), pyee (MIT).
