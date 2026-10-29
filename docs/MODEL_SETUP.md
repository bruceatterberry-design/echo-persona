# Models

All models run on the Mac. None are fine-tuned on the owner's data; the likeness comes from references (voice clip,
photo, approved personality traits) that can be changed without retraining.

| Role | Model | Where | Size | How to get it again |
|---|---|---|---|---|
| Conversation | Llama 3.1 8B (Q4, Ollama) | `~/.ollama/models` | 4.9 GB | `ollama pull llama3.1:8b` |
| Speech recognition (in use) | NVIDIA Parakeet TDT 0.6B v2 (MLX, via mlx-audio, `tts/stt_service.py`) | `~/EchoPersona/models/hf` | 2.5 GB (2.6 GB in memory) | same, repo `mlx-community/parakeet-tdt-0.6b-v2` (run from `~/EchoPersona/tts`); licence CC-BY-4.0 |
| Speech recognition (fallback) | whisper-large-v3-turbo (MLX) | `~/EchoPersona/models/hf` | 1.5 GB | `HF_HOME=~/EchoPersona/models/hf uv run python -c "from huggingface_hub import snapshot_download as d; d('mlx-community/whisper-large-v3-turbo')"`. Loaded only if Parakeet fails, or if `config/voice.json` says `"stt": "whisper"` |
| The owner's voice | Qwen3-TTS 12Hz 0.6B Base (bf16, MLX) | `~/EchoPersona/models/hf` | 2.5 GB | same, repo `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16` (run from `~/EchoPersona/tts`) |
| Face animation (offline) | LivePortrait human weights + InsightFace buffalo_l | `~/EchoPersona/animate/pretrained_weights` | 0.63 GB | `snapshot_download('KlingTeam/LivePortrait', local_dir='pretrained_weights', allow_patterns=['liveportrait/*','insightface/*'], ignore_patterns=['*animal*'])` |
| Face finding | Apple Vision (built into macOS) | — | — | `scripts/build_face_tools.sh` |

## How your likeness is made
- **Voice:** zero-shot cloning from one 5–12 s Avatar Collector recording. `scripts/make_voice_reference.py` picks the cleanest
  kept take in a few categories, copies it to `derived/voice/refs-v1/ref-<category>.wav` (+ transcript in `refs.json`) and
  sets `config/voice.json`. Try another with `uv run python scripts/make_voice_reference.py factual`, then restart the voice service.
- **Face:** LivePortrait renders frames from one photo (the face you mark as preferred on the "My face" page):
  `scripts/build_face.sh` (runs `face/render_face.py atlas5`) → `derived/face/atlas-v5` (6 head poses × 5 expressions × 14 mouth positions × blinks, 415 frames,
  ≈ 10 min). Optional idle motion from your own phone video: `derived/face/motion-v1/idle-<name>.mp4` (`face/motion_post.py`);
  without it the window uses built-in head movement.
  The window switches frames live; no model runs during conversation for the face.
- **Personality:** `personality/build.py` (local LLM) proposes traits with quotes; only the owner-approved ones are used.

## Changing the language model
Pick another Ollama model, `ollama pull <name>`, then set it in `config/echo.yaml`:
```yaml
llm:
  model: <name>
```
Benchmark first (docs/BENCHMARKS.md): with 16 GB shared memory, models much larger than ~8B 4-bit will crowd out the
voice and speech recognition.
