"""Compare speaker-recognition models on the owner's real recordings (enrolled from studio takes, tested on other studio takes
and on phone recordings = a different mic) against other voices (macOS voices + Echo's cloned voice).
Usage: PYTHONPATH=. uv run python scripts/bench_speaker.py <folder with other-voice clips>"""
import random, sys, tempfile, time
from pathlib import Path
import numpy as np
from voice.stt import decode_audio
from voice.speaker import SpeakerID

from core.config import load_config
DATA = load_config().resolve(load_config().dataset_root) / "raw"
others = Path(sys.argv[1])
load = lambda p: decode_audio(Path(p).read_bytes())
studio = sorted((DATA / "voice").glob("*/*.wav")); phone = sorted((DATA / "phone").glob("*/*.wav"))
random.Random(7).shuffle(studio)
enroll, test_studio = studio[:12], studio[12:52]
test_phone = phone[:40]
mac = sorted(others.glob("mac-*")); clone = sorted(others.glob("clone-*"))
clips = {p: load(p) for p in [*enroll, *test_studio, *test_phone, *mac, *clone]}
for model in sorted(Path("models/speaker").glob("*.onnx")):
    sid = SpeakerID(model, tempfile.mkdtemp())
    t = time.time(); sid.enroll("owner", [clips[p] for p in enroll], [p.name for p in enroll])
    def sc(ps): return np.array([sid.scores(clips[p])["owner"] for p in ps if len(clips[p]) >= 16000])
    t0 = time.time(); s_st, s_ph, s_mac, s_cl = sc(test_studio), sc(test_phone), sc(mac), sc(clone)
    n = len(s_st) + len(s_ph) + len(s_mac) + len(s_cl); ms = (time.time() - t0) / n * 1000
    print(f"\n{model.name}: {ms:.0f} ms/clip")
    for k, v in [("the owner studio", s_st), ("the owner phone", s_ph), ("macOS voices", s_mac), ("cloned voice", s_cl)]:
        print(f"  {k:14s} n={len(v):2d} min={v.min():.2f} median={np.median(v):.2f} max={v.max():.2f}")
    for th in (0.4, 0.5, 0.6):
        print(f"  threshold {th}: the owner recognised {np.mean(np.r_[s_st, s_ph] >= th):.0%}, "
              f"macOS voices taken for the owner {np.mean(s_mac >= th):.0%}, clone taken for the owner {np.mean(s_cl >= th):.0%}")
