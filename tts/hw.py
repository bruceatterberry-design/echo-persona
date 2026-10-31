"""Pick model precision from this Mac's memory (the owner, 2026-10-31).

On 16 GB the voice, speech recognition and the LLM fight for the same unified memory and GPU: the 16-bit voice ran at
0.96× real time (stutters whenever the LLM works), 8-bit at 1.61× with 0.85 GB less; speech recognition in bfloat16
uses 1.28 GB instead of 2.51 GB with the same words. the owner chose: up to 16 GB → the lighter versions; 24 GB or more →
full precision. Environment variables override (ECHO_TTS_BITS=16|8, ECHO_STT_DTYPE=f32|bf16).
"""
from __future__ import annotations

import os
import subprocess

BIG_MAC_GB = 24


def memory_gb() -> float:
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout) / 2**30
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        except (ValueError, OSError):
            return 16.0


def big_mac() -> bool:
    return memory_gb() >= BIG_MAC_GB - 0.5


def tts_bits() -> int:
    v = os.environ.get("ECHO_TTS_BITS")
    if v in ("8", "16"):
        return int(v)
    return 16 if big_mac() else 8


def stt_dtype() -> str:
    v = os.environ.get("ECHO_STT_DTYPE")
    if v in ("f32", "bf16"):
        return v
    return "f32" if big_mac() else "bf16"


def describe() -> str:
    return (f"{memory_gb():.0f} GB Mac → voice {tts_bits()}-bit, speech recognition "
            f"{'full precision' if stt_dtype() == 'f32' else 'half precision (bfloat16)'}")


if __name__ == "__main__":
    print(describe())
