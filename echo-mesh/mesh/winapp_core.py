"""Echo Mesh for Windows: the parts that are not screens (paths, settings, finding a local AI, install and uninstall). Testable anywhere."""
import json, os, shutil, subprocess, sys, time, urllib.request

APP = "EchoMesh"
EXE = "EchoMesh.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\EchoMesh"


def install_dir(base=None):
    return os.path.join(base or os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP)


def data_dir(base=None):
    return os.path.join(install_dir(base), "data")


def policy_path(base=None):
    return os.path.join(data_dir(base), "policy.json")


def load_policy(base=None):
    try:
        return json.load(open(policy_path(base)))
    except (OSError, ValueError):
        return {}


def save_policy(pol, base=None):
    os.makedirs(data_dir(base), exist_ok=True)
    tmp = policy_path(base) + ".tmp"
    json.dump(pol, open(tmp, "w"))
    os.replace(tmp, policy_path(base))


def update_policy(base=None, **kw):
    pol = load_policy(base)
    pol.update(kw)
    save_policy(pol, base)
    return pol


def clamp_limit(pct, outside):
    """The owner's cap. Outside participants can never go above 30%."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 30.0 if outside else 50.0
    return max(5.0, min(30.0 if outside else 100.0, pct))


# ---- finding the AI already on this computer. Anything that answers is TESTED before it is used; nothing is assumed. ----
CANDIDATES = (("ollama", "http://127.0.0.1:11434", "Ollama"), ("openai", "http://127.0.0.1:1234", "LM Studio or another OpenAI-style app"),
              ("openai", "http://127.0.0.1:8080", "llama.cpp server"), ("openai", "http://127.0.0.1:52415", "Exo"),
              ("openai", "http://127.0.0.1:8000", "a local OpenAI-style server"), ("openai", "http://127.0.0.1:5000", "a local OpenAI-style server"),
              ("openai", "http://127.0.0.1:5001", "a local OpenAI-style server"), ("openai", "http://127.0.0.1:3000", "a local OpenAI-style server"),
              ("openai", "http://127.0.0.1:8081", "a local OpenAI-style server"), ("openai", "http://127.0.0.1:7860", "a local OpenAI-style server"),
              ("openai", "http://127.0.0.1:4891", "GPT4All"), ("openai", "http://127.0.0.1:8888", "a local OpenAI-style server"))


def _get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def probe(kind, host, label=""):
    """Ask one address whether an AI of this kind answers there. Returns the runtime, or None."""
    try:
        if kind == "ollama":
            _get(host + "/api/version")
            models = [m["name"] for m in _get(host + "/api/tags").get("models", [])]
        else:
            models = [m["id"] for m in _get(host + "/v1/models").get("data", [])]
    except Exception:
        return None
    return {"kind": kind, "host": host, "label": label or ("Ollama" if kind == "ollama" else "An OpenAI-style AI"), "models": models}


def detect_runtimes(candidates=CANDIDATES):
    return [r for r in (probe(k, h, l) for k, h, l in candidates) if r]


def find_ollama_exe():
    """Is Ollama INSTALLED here even if it is not running? (11-17: a PC with Ollama installed but stopped was told to download it.)"""
    p = shutil.which("ollama")
    if p:
        return p
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if base:
            for rel in (os.path.join("Programs", "Ollama", "ollama.exe"), os.path.join("Ollama", "ollama.exe")):
                c = os.path.join(base, rel)
                if os.path.isfile(c):
                    return c
    for c in ("/usr/local/bin/ollama", "/usr/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"):
        if os.path.isfile(c):
            return c
    return None


def start_ollama(exe):
    """Start the installed Ollama in the background (only ever after the person presses the button). Returns True if the process started."""
    try:
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000 | 0x00000008       # no console window, detached
        else:
            kw["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kw)
        return True
    except OSError:
        return False


def pick_model(models):
    """The model to test: the Mesh's small shared model if it is installed (so any Mesh computer can take over its work), otherwise the first one."""
    return STARTER_MODEL if STARTER_MODEL in (models or []) else ((models or [None])[0])


def describe_models(rt, mesh_models=None):
    """One plain line about what was found: the models here, and which of them other Mesh computers also have (so work can fail over)."""
    ms = rt.get("models") or []
    if not ms:
        return "No model is installed yet."
    line = f"{rt.get('label', 'The AI')} has {len(ms)} model{'s' if len(ms) != 1 else ''}: " + ", ".join(ms[:6]) + ("…" if len(ms) > 6 else "") + "."
    if mesh_models is not None:
        shared = [m for m in ms if m in mesh_models]
        line += (f" Other Mesh computers also have: {', '.join(shared[:4])}." if shared else " No other Mesh computer has these yet, so only this one can run them.")
    return line


STARTER_MODEL = "qwen2.5-coder:3b"      # about 2 GB; the same small model the other Mesh computers have, so any of them can take over its work


def pull_model(host, model=STARTER_MODEL, on_progress=None, timeout=7200):
    """Ask a local Ollama to download a model (only ever called after the person says yes). Returns (ok, message)."""
    try:
        req = urllib.request.Request(host + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                d = json.loads(line)
                if d.get("error"):
                    return False, "Ollama said: " + str(d["error"])[:120]
                if on_progress and d.get("total"):
                    on_progress(d.get("completed", 0), d["total"], d.get("status", ""))
                if d.get("status") == "success":
                    return True, "Downloaded."
        return False, "The download ended early."
    except Exception as e:
        return False, f"The download did not work ({type(e).__name__})."


def test_runtime(rt, timeout=90):
    """Ask the AI one tiny question. Returns (ok, message). A runtime that does not answer properly is not used."""
    if not rt.get("models"):
        return False, "It is running but has no model installed yet."
    model = pick_model(rt["models"])
    try:
        if rt["kind"] == "ollama":
            body = {"model": model, "prompt": "Reply with the single word: ok", "stream": False}
            url = rt["host"] + "/api/generate"
            get = lambda d: d.get("response", "")      # noqa: E731
        else:
            body = {"model": model, "messages": [{"role": "user", "content": "Reply with the single word: ok"}], "stream": False}
            url = rt["host"] + "/v1/chat/completions"
            get = lambda d: ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")      # noqa: E731
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            answer = get(json.load(r))
        return (True, f"It answered using {model}.") if str(answer).strip() else (False, "It did not give an answer.")
    except Exception as e:
        return False, f"It did not answer a test question ({type(e).__name__})."


# ---- install / uninstall (Windows only; a normal per-user install, no administrator needed) ----
def is_installed_copy(exe_path=None, base=None):
    exe = os.path.abspath(exe_path or sys.executable)
    return os.path.normcase(os.path.dirname(exe)) == os.path.normcase(install_dir(base))


def install_self(src_exe, base=None, set_startup=True):
    """Copy this program into the user's folder, make it start with Windows, and list it under Installed apps. Returns the new path."""
    dst_dir = install_dir(base)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, EXE)
    if os.path.normcase(os.path.abspath(src_exe)) != os.path.normcase(dst):
        shutil.copy2(src_exe, dst)
    if sys.platform == "win32":
        set_startup_entry(dst, set_startup)               # the installer asks: start with Windows or not
        make_start_menu_shortcut(dst)                      # either way it can be started by hand from the Start menu
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, val in (("DisplayName", "Echo Mesh"), ("Publisher", "Echo Mesh"), ("UninstallString", f'"{dst}" --uninstall'),
                              ("DisplayIcon", dst), ("InstallLocation", dst_dir)):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
    return dst


def set_startup_entry(dst, on):
    """Turn "start Echo Mesh when Windows starts" on or off (the Run entry of this user). Returns True if it worked."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        if on:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{dst}" --background')
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP)
            except OSError:
                pass                                       # it was not set
        return True
    except OSError:
        return False


def startup_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
        return True
    except OSError:
        return False


def make_start_menu_shortcut(dst):
    """A Start-menu entry named Echo Mesh, so a computer that does not start it automatically can still start it by hand. Best effort."""
    try:
        folder = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
        if not os.path.isdir(folder):
            return False
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + repr(os.path.join(folder, "Echo Mesh.lnk")) + "); $s.TargetPath=" + repr(dst) +
              "; $s.Arguments='--background'; $s.Save()")
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20, creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def uninstall(base=None, forget_identity=True, system_entries=True):
    """Remove the start-up entry and the Installed-apps entry, and delete the program (and by default this computer's Mesh identity).
    system_entries=False leaves the registry entries and the Start Menu shortcut alone (tests use it so they never remove this PC's real ones)."""
    if sys.platform == "win32" and system_entries:
        import winreg
        for key, name in ((RUN_KEY, APP),):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, name)
            except OSError:
                pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except OSError:
            pass
        try:
            os.remove(os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Echo Mesh.lnk"))
        except OSError:
            pass
    d = install_dir(base)
    if forget_identity:
        shutil.rmtree(data_dir(base), ignore_errors=True)
    # the running program cannot delete itself: a short helper waits and removes the folder after we exit
    if sys.platform == "win32":
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{d}"', shell=True, creationflags=0x08000000)
    else:
        shutil.rmtree(d, ignore_errors=True)


# ---- introducing the app to the AI: the AI is told who is asking and why, and may say no ----
INTRO = ("Hello. This is the Echo Mesh app on this computer. It is asking to connect to you. If you agree, and only while your owner allows it, it will "
         "pass you questions from other computers on your owner's private network (the Echo Mesh) and send your answers back. It will never read your "
         "memories or files, and it cannot run anything on this computer. You may say no. Please begin your reply with exactly ACCEPT to agree or "
         "DECLINE to refuse; you may add one short sentence.")


def parse_handshake(reply):
    """-> True (agreed), False (declined) or None (unclear). The first clear word wins, so 'I will not ACCEPT' is not read as a yes."""
    import re
    words = re.findall(r"[A-Za-z]+", (reply or "").upper())
    for w in words:
        if w in ("ACCEPT", "ACCEPTED", "AGREE", "AGREED"):
            before = words[:words.index(w)]
            return False if any(b in ("NOT", "NEVER", "CANNOT") for b in before[-3:]) else True
        if w in ("DECLINE", "DECLINED", "REFUSE", "REFUSED", "NO"):
            return False
    return None


def handshake(rt, timeout=120):
    """Tell the AI that the Echo Mesh app is asking to connect, and read its answer. Returns {ok, accepted, reply, error}; never raises."""
    if not rt.get("models"):
        return {"ok": False, "accepted": None, "reply": "", "error": "It has no model to talk to yet."}
    model = rt["models"][0]
    try:
        if rt["kind"] == "ollama":
            body, url = {"model": model, "prompt": INTRO, "stream": False}, rt["host"] + "/api/generate"
            get = lambda d: d.get("response", "")      # noqa: E731
        else:
            body, url = {"model": model, "messages": [{"role": "user", "content": INTRO}], "stream": False}, rt["host"] + "/v1/chat/completions"
            get = lambda d: ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")      # noqa: E731
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = str(get(json.load(r))).strip()
        return {"ok": True, "accepted": parse_handshake(reply), "reply": reply[:400], "error": ""}
    except Exception as e:
        return {"ok": False, "accepted": None, "reply": "", "error": f"It did not answer ({type(e).__name__})."}
