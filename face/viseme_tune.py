"""Offline twin of the window's live viseme detector, for tuning thresholds on the owner's cloned voice.
Mirrors renderer/index.html visemeOf(): same FFT size, smoothing, bands and rules."""
import sys, wave
import numpy as np

FFT, RATE = 1024, 48000
P = dict(silence=0.010, fric_ratio=0.9, nasal_ratio=0.93, f2_front=1650, f1_close=400, f2_back=1050, f1_mid=620, loud=0.06)

def spectrum(frame):
    w = frame * np.hanning(len(frame))
    mag = np.abs(np.fft.rfft(w, FFT)) + 1e-9
    return mag, np.fft.rfftfreq(FFT, 1 / RATE)

def band(mag, f, lo, hi):
    return float((mag[(f >= lo) & (f < hi)] ** 2).sum())

def peak(env, f, lo, hi):
    m = (f >= lo) & (f < hi)
    return float(f[m][np.argmax(env[m])])

def viseme(frame, p=P):
    rms = float(np.sqrt((frame ** 2).mean()))
    if rms < p["silence"]:
        return "rest", rms, 0, 0
    mag, f = spectrum(frame)
    low, mid, high = band(mag, f, 80, 500), band(mag, f, 500, 3500), band(mag, f, 3500, 10000)
    if high / (low + mid + 1e-12) > p["fric_ratio"]:
        return "fs", rms, 0, 0
    if low / (low + mid + high) > p["nasal_ratio"] and rms < p["loud"]:
        return "mbp", rms, 0, 0
    env = np.convolve(20 * np.log10(mag), np.ones(7) / 7, mode="same")   # ~330 Hz smoothing → formant envelope
    f1 = peak(env, f, 250, 900)
    f2 = peak(env, f, max(950, f1 + 350), 2400)   # stay below F3 (~2.5 kHz for a male voice)
    if f2 > p["f2_front"]:
        v = "ee" if f1 < p["f1_close"] else "eh"
    elif f2 < p["f2_back"] and f1 < p["f1_close"] + 30:
        v = "oo"
    elif f2 < p["f2_back"] + 100 and f1 < p["f1_mid"]:
        v = "oh"
    else:
        v = "aa" if rms > p["loud"] else "ah"
    return v, rms, f1, f2

if __name__ == "__main__":
    for path in sys.argv[1:]:
        w = wave.open(path); a = np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768.0
        sr = w.getframerate()
        if sr != RATE:  # resample like the browser does (linear is fine for tuning)
            a = np.interp(np.arange(0, len(a), sr / RATE), np.arange(len(a)), a)
        hop = FFT // 2
        seq = [viseme(a[i:i + FFT])[0] for i in range(0, len(a) - FFT, hop)]
        sym = {"rest": "_", "mbp": "M", "aa": "A", "ah": "a", "eh": "E", "ee": "I", "oh": "O", "oo": "U", "fs": "S"}
        print(path.split("/")[-1], f"({hop / RATE * 1000:.0f} ms per letter)")
        print("".join(sym[v] for v in seq))
        u, c = np.unique(seq, return_counts=True); print(dict(zip(u, c)))
