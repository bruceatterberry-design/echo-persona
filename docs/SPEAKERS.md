# Who's talking (speaker recognition)

Echo tells the owner's voice from the loved one's (and from voices it doesn't know), on this Mac only.

- **Library:** sherpa-onnx 2.0.1 (Apache-2.0, k2-fsa), CPU, about 0.1 s per phrase.
- **Model:** `models/speaker/wespeaker_en_voxceleb_resnet34.onnx` (26.5 MB, sha256 `5ef208a9…57fe94`). Download:
  `mkdir -p models/speaker && curl -L -o models/speaker/wespeaker_en_voxceleb_resnet34.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx`
  Check the WeSpeaker project's licence for the model weights before redistributing them.
- **Profiles:** `data/speakers/<name>.npz` (voice prints and which recordings they came from; not in git).
  - **Owner** (`owner`): built from your own recordings with `PYTHONPATH=. uv run python -m voice.enroll --replace`.
  - **Loved one** (`loved_one`): recorded on the "Who's talking" page (`/voices.html`) after they tick the consent box.
    Originals are kept read-only in `<dataset>/raw/speakers/loved_one/` with sha256. "Forget this voice" deletes the
    profile and the originals.
- **Decision:** a phrase belongs to a person when similarity ≥ 0.72 *and* it is ≥ 0.10 above the next person; otherwise
  "unknown". Phrases under 1 s are always "unknown". Settings: `speaker_threshold` / `speaker_margin` in `core/config.py`.
- **Where it's used:**
  - `/api/stt` returns `speaker` with any transcript that's kept (never without the wake phrase in wake mode).
  - The window sends it with the message.
  - The LLM gets "[Recognised by voice: … is speaking]" with that message only; the stored conversation keeps just the words.
  - "Remember that …" said by the loved one is saved with told_by `loved_one`.
  - Echo only asks the owner about their life when it hears the owner's voice (`memory/curiosity.py`).

## Measured on the developer's Mac (scripts/bench_speaker.py, 2026-10-30)

Profile: 12 studio takes from one person. Tested on 40 other studio takes and 40 phone takes (a different mic), plus other voices.

| | WeSpeaker ResNet34 (chosen) | 3D-Speaker CAM++ |
|---|---|---|
| Owner, studio mic | 0.73–0.96 | 0.45–0.89 |
| Owner, phone mic | 0.80–0.92 | 0.34–0.84 |
| 10 macOS voices | 0.52–0.70 | 0.09–0.72 |
| The owner's cloned voice | 0.82–0.88 | 0.38–0.72 |

## Limits

- Tested on one real speaker plus synthetic voices. Try it on your own household with "Try my voice" before relying on it.
- The owner's cloned voice, or a recording of the owner, counts as the owner. Echo doesn't listen while it speaks
  (half-duplex + echo holdoff), so it shouldn't hear itself. This is not a security feature.
