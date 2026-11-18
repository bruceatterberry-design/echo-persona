"""What this node can actually do. Facts only; nothing here is assumed."""
import json, os, platform, shutil, subprocess, urllib.request


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def ram_bytes():
    try:
        s = platform.system()
        if s == "Darwin":
            return int(_run(["sysctl", "-n", "hw.memsize"]))
        if s == "Linux":
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        if s == "Windows":
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("tpv", ctypes.c_ulonglong),
                            ("apv", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            m = M(); m.l = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.tp)
    except Exception:
        pass
    return None


def gpus():
    out = []
    if shutil.which("nvidia-smi"):
        for line in _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]).splitlines():
            out.append({"name": line.split(",")[0].strip(), "vram": line.split(",", 1)[1].strip() if "," in line else None, "source": "nvidia-smi"})
    return out   # AMD/Intel/Apple GPUs are not guessed here; they are filled in from owner-verified inventory


def ollama(host="http://127.0.0.1:11434"):
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.load(r).get("models", [])]
        with urllib.request.urlopen(host + "/api/version", timeout=3) as r:
            ver = json.load(r).get("version")
        return {"host": host, "version": ver, "models": models}
    except Exception:
        return {"host": host, "version": None, "models": []}


def openai_style(host):
    """A local AI that answers like the OpenAI API (LM Studio, llama.cpp server, Exo...). Tested, never assumed."""
    try:
        with urllib.request.urlopen(host + "/v1/models", timeout=3) as r:
            models = [m["id"] for m in json.load(r).get("data", [])]
        return {"host": host, "version": None, "models": models, "kind": "openai"}
    except Exception:
        return {"host": host, "version": None, "models": [], "kind": "openai"}


_MID = []


def machine_id():
    """A stable id for THIS computer, hashed so the raw system id never leaves it. Same computer -> same id after a reinstall; a copied
    install on another computer -> a different id (that is how a cloned identity is noticed). None if nothing stable can be read."""
    import hashlib
    raw = ""
    try:
        s = platform.system()
        if s == "Linux":
            for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(f):
                    raw = open(f).read().strip(); break
        elif s == "Darwin":
            for line in _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]).splitlines():
                if "IOPlatformUUID" in line:
                    raw = line.split('"')[-2]; break
        elif s == "Windows":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                raw = winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        raw = ""
    return hashlib.sha256(("echo-mesh|" + raw).encode()).hexdigest()[:24] if raw else None


def machine_id_cached():
    if not _MID:
        _MID.append(machine_id())
    return _MID[0]


def collect(ollama_host="http://127.0.0.1:11434", kind="ollama"):
    # The key stays "ollama" for compatibility: it holds the models of whichever local AI the owner connected.
    return {"os": f"{platform.system()} {platform.release()}", "machine": platform.machine(), "cpu_threads": os.cpu_count(),
            "ram_bytes": ram_bytes(), "gpus": gpus(), "machine_id": machine_id_cached(), "ollama": openai_style(ollama_host) if kind == "openai" else ollama(ollama_host)}


# ---- live measurements: ONE function for every computer, the Mac coordinator included (dashboard: CPU, memory, GPU, latency) ----
# Anything that cannot be measured on this computer is None ("not measured"), never guessed.
_cpu_prev = {}


def parse_meminfo(text):
    """Linux /proc/meminfo -> (used_bytes, total_bytes)."""
    v = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in text.splitlines() if ":" in l and len(l.split()) >= 2 and l.split()[1].isdigit()}
    if "MemTotal" in v and "MemAvailable" in v:
        return v["MemTotal"] - v["MemAvailable"], v["MemTotal"]
    return None, v.get("MemTotal")


def parse_vm_stat(text, total):
    """macOS vm_stat -> used bytes the way Activity Monitor counts it (active + wired + compressed)."""
    import re
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 16384
    def pages(label):
        r = re.search(rf"{label}:\s+(\d+)\.", text)
        return int(r.group(1)) if r else 0
    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
    return (used, total) if total else (used, None)


def memory():
    """-> (used_bytes, total_bytes), either may be None."""
    try:
        s = platform.system()
        if s == "Linux":
            return parse_meminfo(open("/proc/meminfo").read())
        if s == "Darwin":
            return parse_vm_stat(_run(["vm_stat"]), ram_bytes())
        if s == "Windows":
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("tpv", ctypes.c_ulonglong),
                            ("apv", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            m = M(); m.l = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.tp - m.ap), int(m.tp)
    except Exception:
        pass
    return None, None


def cpu_percent():
    """Whole-computer CPU use since the last call (Linux, Windows), or the sum of process use (macOS). None the first time or if unknown."""
    try:
        s = platform.system()
        if s == "Darwin":
            tot = sum(float(x) for x in _run(["ps", "-A", "-o", "%cpu"]).splitlines()[1:] if x.strip())
            return round(min(100.0, tot / (os.cpu_count() or 1)), 1)
        if s == "Linux":
            f = open("/proc/stat").readline().split()[1:]
            vals = [int(x) for x in f]; idle, total = vals[3] + vals[4], sum(vals)
        elif s == "Windows":
            import ctypes
            from ctypes import wintypes
            i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
            q = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
            idle, total = q(i), q(k) + q(u)          # kernel time already includes idle
        else:
            return None
        prev = _cpu_prev.get("v"); _cpu_prev["v"] = (idle, total)
        if not prev or total <= prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)
    except Exception:
        return None


def gpu_use():
    """First NVIDIA GPU's use % and memory used (MB) from nvidia-smi; None where there is no way to measure (AMD/Intel/Apple are not guessed)."""
    if not shutil.which("nvidia-smi"):
        return None, None
    try:
        a, b = [x.strip() for x in _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"]).splitlines()[0].split(",")]
        return float(a), float(b)
    except Exception:
        return None, None


def load():
    out = {}
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        pass   # Windows: not available without extra libraries; reported as unknown, not guessed
    used, total = memory()
    cpu = cpu_percent(); g, gm = gpu_use()
    for k, v in (("cpu_pct", cpu), ("mem_used_bytes", used), ("mem_total_bytes", total), ("gpu_pct", g), ("gpu_mem_mb", gm)):
        if v is not None:
            out[k] = v
    return out
