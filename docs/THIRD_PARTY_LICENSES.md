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
