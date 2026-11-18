"""Echo Mesh for Windows: setup wizard, tray icon and status window in one program (standard library + tkinter; pystray and Pillow for the tray)."""
import os, queue, socket, sys, threading, time, traceback, webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

from . import VERSION, COORD_PORT
from .agent import Agent, discover
from . import winapp_core as W
from .agreement import AGREEMENT, CONSENT_TEXT, READ_TEXT, TERMS, TERMS_READ, TERMS_VERSION
from .common import ROLES
from .splash import draw_splash
from . import dashboard as D, iconpng, speedup

BG, FG, DIM, ACC = "#f3f5fa", "#1b2130", "#596380", "#3b5fd0"
FONT = ("Segoe UI", 11)


def parse_address(text):
    """'sweetie.example.net' or 'host:47821' -> (host, port). Returns None if it is not an address."""
    t = (text or "").strip().replace("https://", "").replace("http://", "").strip("/")
    if not t or " " in t:
        return None
    host, _, port = t.partition(":")
    if not host or (port and not port.isdigit()):
        return None
    return host, int(port) if port else COORD_PORT


class Dispatcher:
    """Tk may only be touched from its own thread. Other threads (network work, the tray icon) post a function here and the window's
    thread runs it a moment later."""
    def __init__(self, root):
        self.root, self.q = root, queue.Queue()
        root.after(50, self._drain)

    def post(self, fn, *a):
        self.q.put((fn, a))

    def _drain(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except tk.TclError:
                    pass                       # the window was closed meanwhile
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._drain)
        except tk.TclError:
            pass


class Wizard(tk.Tk):
    def __init__(self, base=None, address="", frozen=False, splash=True):
        super().__init__()
        self.base, self.address, self.frozen = base, address, frozen
        self.role = None            # "worker" or "peer", chosen on the configuration step
        self.title("Echo Mesh setup")
        self._photo = icon_photo()
        if self._photo:
            try: self.iconphoto(True, self._photo)
            except tk.TclError: pass
        self.geometry("620x560")
        self.configure(bg=BG)
        self.agent = Agent(W.data_dir(base))
        self.disp = Dispatcher(self)
        self.mode = None            # "home" or "invite"
        self.outside = False
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=24, pady=20)
        if self.agent.ident:                                   # already joined on an earlier run: carry on, do not ask again
            self.step_welcome()
            threading.Thread(target=self._resume_check, daemon=True).start()
        elif splash:
            self.step_splash()                                 # splash -> terms -> configuration -> where is the Mesh
        else:
            self.step_terms()

    def _resume_check(self):
        try:
            st, _ = self.agent.heartbeat_once()
        except Exception:
            return
        if st == 200:
            self.ui(self._resume)

    def _resume(self):
        self.outside = bool(W.load_policy(self.base).get("outside", False)) or not self.agent.ident.get("host", "").startswith(("10.", "192.168.", "172.", "127."))
        self.head("Welcome back", "This computer has already joined the Mesh. Finishing setup…")
        self.after(1200, self.step_ai)

    # ---- helpers ----
    def clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def head(self, title, sub=""):
        self.clear()
        tk.Label(self.body, text=title, font=("Segoe UI", 18, "bold"), bg=BG, fg=FG, anchor="w").pack(fill="x")
        if sub:
            tk.Label(self.body, text=sub, font=FONT, bg=BG, fg=DIM, anchor="w", justify="left", wraplength=560).pack(fill="x", pady=(4, 12))

    def text(self, s, color=FG, bold=False):
        l = tk.Label(self.body, text=s, font=FONT if not bold else ("Segoe UI", 11, "bold"), bg=BG, fg=color, anchor="w", justify="left", wraplength=560)
        l.pack(fill="x", pady=4)
        return l

    def button(self, label, cmd, primary=False, state="normal", parent=None):
        b = tk.Button(parent or self.body, text=label, command=cmd, font=FONT, padx=16, pady=8, state=state,
                      bg=ACC if primary else "white", fg="white" if primary else FG, relief="flat", bd=1, highlightthickness=0)
        return b

    def row(self):
        r = tk.Frame(self.body, bg=BG)
        r.pack(fill="x", pady=10)
        return r

    def ui(self, fn, *a):
        self.disp.post(fn, *a)               # safe from any thread

    def cancel(self):
        self.agent.stop.set()
        self.destroy()

    # ---- steps ----
    def step_splash(self, ms=2800):
        """Full-screen splash: the ring draws itself while the installer wakes up (the same drawing as the preview picture). Click or Esc skips it."""
        self.withdraw()
        top = tk.Toplevel(self)
        top.configure(bg="#050a18")
        top.attributes("-fullscreen", True)
        try: top.attributes("-topmost", True)
        except tk.TclError: pass
        w, h = top.winfo_screenwidth(), top.winfo_screenheight()
        cv = tk.Canvas(top, width=w, height=h, bg="#050a18", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        done = []
        def finish(_e=None):
            if done: return
            done.append(1)
            top.destroy(); self.deiconify(); self.step_terms()
        top.bind("<Button-1>", finish); top.bind("<Escape>", finish); top.focus_force()
        t0 = time.time()
        def frame():
            if done: return
            k = min(1.0, (time.time() - t0) * 1000.0 / ms)
            cv.delete("all")
            draw_splash(D.TkCtx(cv, self._photo), w, h, sweep=max(1.0, 360.0 * k))
            if k >= 1.0:
                return top.after(500, finish)
            top.after(45, frame)
        frame()

    def _legal(self, title, sub, text, read_label, consent_label=None, on_continue=None, decline=False):
        """A scroll box that must be read to the end, then one or two tick boxes (the program enforces them, not the screen). Used for the terms and for the outside participation agreement."""
        self.head(title, sub)
        box = tk.Frame(self.body, bg=BG)
        box.pack(fill="both", expand=True)
        t = tk.Text(box, wrap="word", font=("Segoe UI", 10), height=14, bg="white", fg=FG, relief="solid", bd=1)
        sb = ttk.Scrollbar(box, command=t.yview)
        t.configure(yscrollcommand=lambda a, b: (sb.set(a, b), self._scrolled(float(b))))
        t.insert("1.0", text)
        t.configure(state="disabled")
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.v1, self.v2 = tk.BooleanVar(), tk.BooleanVar(value=consent_label is None)
        self.c1 = tk.Checkbutton(self.body, text=read_label, variable=self.v1, bg=BG, fg=FG, font=FONT, anchor="w", justify="left", wraplength=540, state="disabled", command=self._boxes)
        self.c1.pack(fill="x", pady=(8, 0))
        self.c2 = None
        if consent_label:
            self.c2 = tk.Checkbutton(self.body, text=consent_label, variable=self.v2, bg=BG, fg=FG, font=FONT, anchor="w", justify="left", wraplength=540, state="disabled", command=self._boxes)
            self.c2.pack(fill="x")
        bar = tk.Frame(self.body, bg=BG); bar.pack(fill="x", pady=8)
        self.next_btn = self.button("Continue", on_continue, primary=True, state="disabled", parent=bar)
        self.next_btn.pack(side="right")
        if decline:
            self.button("Decline and close", self.cancel, parent=bar).pack(side="left")
        self.read_to_end = False

    def step_terms(self):
        self._legal("Terms and disclaimer", "Please read all of it. The box switches on when you reach the end.", TERMS, TERMS_READ, None, self.after_terms, decline=True)

    def after_terms(self):
        if not (self.read_to_end and self.v1.get()):
            return
        W.save_policy({**W.load_policy(self.base), "terms_accepted": {"version": TERMS_VERSION, "at": time.strftime("%Y-%m-%d %H:%M:%S")}}, self.base)   # kept on this computer
        self.step_config()

    def step_config(self):
        self.head("Choose this computer's Mesh configuration", "You can change this later from the tray icon. The owner of the Mesh still approves every computer.")
        self.cfg = tk.StringVar(value="worker")
        choices = (("worker", "Worker node  (recommended)", "Shares this computer's spare power for jobs the Mesh sends. This is the right choice for a computer without its own AI, such as the living-room PC."),
                   ("peer", "Peer node  (has its own AI)", "This computer already runs an AI of its own, such as a partner AI. It keeps its own identity, and on its own dashboard it is the Primary while the Mesh owner's system appears as the Hub."))
        for val, title, blurb in choices:
            card = tk.Frame(self.body, bg="white", highlightbackground="#c9d3ee", highlightthickness=1)
            card.pack(fill="x", pady=6)
            tk.Radiobutton(card, text=title, variable=self.cfg, value=val, bg="white", fg=FG, font=("Segoe UI", 12, "bold"), anchor="w", activebackground="white").pack(fill="x", padx=10, pady=(8, 0))
            tk.Label(card, text=blurb, bg="white", fg=DIM, font=FONT, anchor="w", justify="left", wraplength=520).pack(fill="x", padx=34, pady=(0, 10))
        self.autostart = tk.BooleanVar(value=True)
        tk.Checkbutton(self.body, text="Start Echo Mesh automatically when this computer starts (recommended)", variable=self.autostart, bg=BG, fg=FG, font=FONT,
                       anchor="w", justify="left", wraplength=540).pack(fill="x", pady=(8, 0))
        tk.Label(self.body, text="If you leave it off, start it from the Start menu (Echo Mesh) whenever you want it sharing. You can change this later from the tray icon.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=540).pack(fill="x", padx=24)
        self.button("Continue", self.after_config, primary=True).pack(anchor="e", pady=8)

    def after_config(self):
        self.role = self.cfg.get() if self.cfg.get() in ROLES else "worker"
        W.save_policy({**W.load_policy(self.base), "role": self.role, "autostart": bool(self.autostart.get())}, self.base)      # the agent reports the role with its inventory
        self.step_welcome()

    def step_welcome(self):
        self.head("Echo Mesh", "Share spare computing time with a private network of computers. You stay in control: pause or remove it whenever you like.")
        self.text("Is this computer on the same home network as the Mesh computer, or did someone invite you from outside?")
        r = self.row()
        self.button("Same home network (automatic)", lambda: self.start("home"), primary=True).pack(side="left", padx=(0, 10))
        self.button("I was invited (I have a pairing code)", lambda: self.start("invite")).pack(side="left")

    def start(self, mode):
        self.mode, self.outside = mode, mode == "invite"
        self.step_home() if mode == "home" else self.step_agreement()

    # home network: find the Mesh, ask to join, show the matching number
    def step_home(self):
        self.head("Looking for the Mesh", "This computer will ask to join. You then approve it on the Mesh computer.")
        self.status = self.text("Looking on your home network…", DIM)
        self.numlabel = None
        threading.Thread(target=self._home_worker, daemon=True).start()

    def _home_worker(self):
        found = None
        for _ in range(3):
            found = discover(timeout=3)
            if found:
                break
        if not found:
            return self.ui(self.fail, "I couldn't find the Mesh on this network. Is the Mesh computer on, and is this computer on the same network? Then try again.", self.step_welcome)
        host, port, _ = found
        self.ui(lambda: self.status.config(text="Found the Mesh. Asking to join…"))
        try:
            self.agent.join_lan(host, port, socket.gethostname(), on_number=lambda n: self.ui(self.show_number, n), timeout=600)
        except Exception as e:
            return self.ui(self.fail, str(e), self.step_welcome)
        self.ui(self.step_ai)

    def show_number(self, n):
        self.status.config(text="Now check the Mesh computer. It should show this same number. If it doesn't, press No there.")
        tk.Label(self.body, text=n, font=("Consolas", 54, "bold"), bg=BG, fg=FG).pack(pady=14)
        self.text("Waiting for you to approve it on the Mesh computer…", DIM)

    # invited from outside: agreement, both boxes, address + pairing code
    def step_agreement(self):
        self._legal("Participation agreement", "Please read all of it. The boxes below switch on when you reach the end.", AGREEMENT, READ_TEXT, CONSENT_TEXT, self.step_code)

    def _scrolled(self, bottom):
        if bottom >= 0.999 and not self.read_to_end:
            self.read_to_end = True
            self.c1.config(state="normal")
            if self.c2 is not None: self.c2.config(state="normal")

    def _boxes(self):
        both = self.read_to_end and self.v1.get() and self.v2.get()          # the program, not the screen, enforces both agreements
        self.next_btn.config(state="normal" if both else "disabled")

    def step_code(self):
        if not (self.read_to_end and self.v1.get() and self.v2.get()):
            return
        self.head("Connect", "Type the 4-digit pairing code you were given." if self.address else "Type the Mesh address and the 4-digit pairing code you were given.")
        self.address_locked = bool(self.address)
        if self.address_locked:
            self.addr = None                                  # the address came with the program: nobody is asked for it
        else:
            tk.Label(self.body, text="Mesh address", bg=BG, fg=DIM, font=FONT, anchor="w").pack(fill="x")
            self.addr = tk.Entry(self.body, font=FONT); self.addr.pack(fill="x", pady=(0, 10))
        tk.Label(self.body, text="Pairing code", bg=BG, fg=DIM, font=FONT, anchor="w").pack(fill="x")
        self.code = tk.Entry(self.body, font=("Consolas", 20), width=6); self.code.pack(anchor="w", pady=(0, 10))
        self.status = self.text("", DIM)
        self.go = self.button("Connect", self.connect, primary=True); self.go.pack(anchor="e", pady=8)

    def connect(self):
        a, code = parse_address(self.address if self.address_locked else self.addr.get()), self.code.get().strip()
        if not a or not (code.isdigit() and len(code) == 4):
            return self.status.config(text="Type the 4 digits of the code." if self.address_locked else "Check the address and type the 4 digits of the code.", fg="#b3372f")
        self.go.config(state="disabled")
        self.status.config(text="Connecting…", fg=DIM)
        threading.Thread(target=self._invite_worker, args=a + (code,), daemon=True).start()

    def _invite_worker(self, host, port, code):
        try:
            r = self.agent.enroll(host, port, code, socket.gethostname())
            if r["status"] == "pending":
                self.ui(lambda: self.status.config(text="Connected. Waiting for the Mesh owner to approve this computer… (you can leave this window open)", fg=DIM))
                end = time.time() + 1800
                while time.time() < end and not self.agent.stop.is_set():
                    st, _ = self.agent.heartbeat_once()
                    if st == 200:
                        break
                    if st not in (403,):
                        raise RuntimeError("The Mesh stopped answering.")
                    time.sleep(3)
                else:
                    raise RuntimeError("Nobody approved this computer in time. Ask for a new code.")
        except Exception as e:
            if isinstance(e, (OSError, TimeoutError)) or "timed out" in str(e).lower():       # the network first: "connection refused" is not a wrong code
                msg = "I couldn't reach the Mesh at that address. If you were told to use Tailscale, install it and sign in first, then try again."
                self.ui(self._offer_tailscale)
            elif "refused" in str(e):
                msg = "The code was not accepted. Check it, or ask for a new one."
            else:
                msg = str(e)
            return self.ui(self._code_failed, msg)
        self.ui(self.step_ai)

    def _offer_tailscale(self):
        if not getattr(self, "ts_btn", None) or not self.ts_btn.winfo_exists():
            self.ts_btn = self.button("Get Tailscale (opens the download page)", lambda: webbrowser.open("https://tailscale.com/download"))
            self.ts_btn.pack(anchor="w", pady=4)

    def _code_failed(self, msg):
        self.status.config(text=msg, fg="#b3372f")
        self.go.config(state="normal")

    # the AI already on this computer
    def step_ai(self):
        self.ai_gen = getattr(self, "ai_gen", 0) + 1
        self.head("The AI on this computer", "The Mesh sends this computer's AI questions to answer. Echo Mesh looks for one by itself and tests it with one tiny question before using it.")
        self.status = self.text("Looking for an AI on this computer…", DIM)
        self._start_scan()

    def _start_scan(self):
        gen = self.ai_gen
        threading.Thread(target=lambda: self.ui(self._ai_found, W.detect_runtimes(), gen), daemon=True).start()

    def _clear_below_status(self):
        kids = self.body.winfo_children()
        for w in kids[kids.index(self.status) + 1:]:
            w.destroy()

    def _ai_found(self, found, gen):
        if gen != getattr(self, "ai_gen", 0):
            return                                          # the person has moved on
        self._clear_below_status()
        usable = [r for r in found if r["models"]]
        if len(usable) == 1 and not self.outside:
            self.text(W.describe_models(usable[0], self._mesh_models()), DIM)        # say what was found before testing it
            return self.use_runtime(usable[0])              # your own computer with one AI: no clicking needed
        if usable:
            return self._pick_runtime(usable)
        if found and not self.outside and any(r["kind"] == "ollama" for r in found):
            return self._offer_model([r for r in found if r["kind"] == "ollama"][0])
        if self.outside:
            return self._manual_runtime()
        exe = W.find_ollama_exe()
        if exe:                                              # installed but not running: do not send the person to the download page
            self.status.config(text="Ollama is installed on this computer, but it isn't running.")
            self.text("Press Start Ollama and I'll carry on by myself. Your models stay as they are; nothing is downloaded.", DIM)
            r = self.row()
            def start():
                ok = W.start_ollama(exe)
                self.status.config(text="Starting Ollama…" if ok else "I couldn't start Ollama. Start it yourself, then I'll notice.")
                self.after(4000, lambda: self._rescan(gen))
            self.button("Start Ollama", start, primary=True).pack(side="left", padx=(0, 10))
            self.button("Skip for now", self.after_ai).pack(side="left")
            return self.after(3000, lambda: self._rescan(gen))
        self.status.config(text="I didn't find an AI on this computer yet.")
        self.text("If you want this computer to answer questions for the Mesh, install Ollama. I'll notice when it is running and carry on by myself.", DIM)
        r = self.row()
        self.button("Get Ollama (opens the download page)", lambda: webbrowser.open("https://ollama.com/download"), primary=True).pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")
        self.after(3000, lambda: self._rescan(gen))

    def _mesh_models(self):
        """The models the OTHER Mesh computers have (best effort: None if the Mesh can't be asked, so nothing is claimed)."""
        try:
            st, ov = self.agent.client().request("GET", "/overview", None, timeout=5)
            if st == 200:
                me = self.agent.ident.get("node_id")
                return {m for n in ov.get("nodes", []) if not n.get("is_me") for m in n.get("models", [])}
        except Exception:
            pass
        return None

    def _rescan(self, gen):
        if gen == getattr(self, "ai_gen", 0):
            self._start_scan()

    def _offer_model(self, rt):
        self.status.config(text="Ollama is running, but it has no AI model yet.")
        self.text(f"I can download a small starter model ({W.STARTER_MODEL}, about 2 GB). It runs on this computer only.", DIM)
        self.bar = ttk.Progressbar(self.body, length=420, mode="determinate")
        self.bar_label = self.text("", DIM)
        r = self.row()
        self.get_btn = self.button("Download the starter model", lambda: self._pull(rt), primary=True)
        self.get_btn.pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")

    def _pull(self, rt):
        self.get_btn.config(state="disabled")
        self.bar.pack(pady=6)
        gen = self.ai_gen
        def progress(done, total, status):
            self.ui(lambda: (self.bar.config(value=100.0 * done / total), self.bar_label.config(text=f"{status}  {done / 1e9:.2f} of {total / 1e9:.2f} GB")))
        def work():
            ok, msg = W.pull_model(rt["host"], on_progress=progress)
            self.ui(lambda: self._start_scan() if ok else (self.bar_label.config(text=msg, fg="#b3372f"), self.get_btn.config(state="normal")))
        threading.Thread(target=work, daemon=True).start()

    def _pick_runtime(self, usable):
        self.status.config(text="Found these. Choose the one to connect:" if self.outside else "Found more than one. Choose the one to connect:")
        self.runtimes = usable
        self.choice = tk.IntVar(value=0)
        for i, rt in enumerate(usable):
            tk.Radiobutton(self.body, text=f"{rt['label']}  ·  {len(rt['models'])} model(s): {', '.join(rt['models'][:3])}", variable=self.choice, value=i,
                           bg=BG, fg=FG, font=FONT, anchor="w", wraplength=540, justify="left").pack(fill="x", pady=3)
        self.note = self.text("", DIM)
        r = self.row()
        self.use = self.button("Test and use this one", lambda: self.use_runtime(self.runtimes[self.choice.get()]), primary=True)
        self.use.pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")

    def _manual_runtime(self):
        self.status.config(text="I couldn't find your AI by myself. Tell me where it listens (you can leave this for later).")
        self.kind_var = tk.StringVar(value="openai")
        for val, label in (("openai", "It answers like the OpenAI API (LM Studio, llama.cpp, many others)"), ("ollama", "It is Ollama")):
            tk.Radiobutton(self.body, text=label, variable=self.kind_var, value=val, bg=BG, fg=FG, font=FONT, anchor="w").pack(fill="x", pady=2)
        tk.Label(self.body, text="Address, for example http://127.0.0.1:1234", bg=BG, fg=DIM, font=FONT, anchor="w").pack(fill="x", pady=(8, 0))
        self.man_addr = tk.Entry(self.body, font=FONT); self.man_addr.insert(0, "http://127.0.0.1:"); self.man_addr.pack(fill="x")
        self.note = self.text("", DIM)
        r = self.row()
        self.use = self.button("Test and use it", self.use_manual, primary=True); self.use.pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")

    def use_manual(self):
        host = self.man_addr.get().strip().rstrip("/")
        if not host.startswith("http"):
            return self.note.config(text="The address should start with http://", fg="#b3372f")
        rt = W.probe(self.kind_var.get(), host)
        if not rt:
            return self.note.config(text="Nothing answered there. Check the address and that the AI is running.", fg="#b3372f")
        self.use_runtime(rt)

    def use_runtime(self, rt):
        """Home computer: test the AI with one tiny question. Outside computer: introduce the app to the AI and let the AI say yes or no."""
        if self.outside:
            return self._introduce(rt)
        if hasattr(self, "use"):
            try: self.use.config(state="disabled")
            except tk.TclError: pass
        try:
            self.status.config(text=f"Testing {rt['label']}…", fg=DIM)
        except tk.TclError:
            pass
        def work():
            ok, msg = W.test_runtime(rt)
            if ok:
                W.update_policy(self.base, runtime={"kind": rt["kind"], "host": rt["host"]})
                self._speed_models = list(rt["models"]) if rt["kind"] == "ollama" else []      # the speed-up step is offered only for your own Ollama
            self.ui(lambda: self.after_ai() if ok else self._runtime_failed(rt, msg))
        threading.Thread(target=work, daemon=True).start()

    def _introduce(self, rt):
        self.head(f"Introducing Echo Mesh to {rt['label']}", "Echo Mesh is telling your AI that it is asking to connect, and why. Your AI answers yes or no.")
        self.status = self.text("Waiting for your AI's answer…", DIM)
        def work():
            h = W.handshake(rt)
            self.ui(self._introduced, rt, h)
        threading.Thread(target=work, daemon=True).start()

    def _introduced(self, rt, h):
        def save(owner_override=False):
            W.update_policy(self.base, runtime={"kind": rt["kind"], "host": rt["host"]},
                            handshake={"accepted": h["accepted"], "reply": h["reply"][:200], "at": time.time(), "owner_confirmed": owner_override})
            self.after_ai()
        if not h["ok"]:
            return self._runtime_failed(rt, h["error"])
        self.head(f"{rt['label']} answered")
        self.text("Its reply:", DIM)
        tk.Label(self.body, text=h["reply"] or "(no words)", font=("Segoe UI", 11, "italic"), bg="white", fg=FG, wraplength=540, justify="left", anchor="w", padx=12, pady=10).pack(fill="x", pady=6)
        r = self.row()
        if h["accepted"] is True:
            self.text("Your AI agreed to connect.", "#1d8a4c", True)
            self.button("Connect it", save, primary=True).pack(side="left", padx=(0, 10))
        elif h["accepted"] is False:
            self.text("Your AI said no, so it will not be connected.", "#b3372f", True)
            self.button("Choose another", self.step_ai, primary=True).pack(side="left", padx=(0, 10))
        else:
            self.text("Your AI didn't clearly say yes or no. You decide.", "#a06a14", True)
            self.button("Connect anyway", lambda: save(True), primary=True).pack(side="left", padx=(0, 10))
            self.button("Don't connect", self.step_ai).pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")

    def _runtime_failed(self, rt, msg):
        self.head("That AI didn't answer properly", msg)
        r = self.row()
        self.button("Try again", self.step_ai, primary=True).pack(side="left", padx=(0, 10))
        self.button("Skip for now", self.after_ai).pack(side="left")

    # after the AI step: your own computers are done; an outside computer chooses its limit first
    def after_ai(self):
        if getattr(self, "_speed_models", None) and not getattr(self, "_speed_done", False) and speedup.find_llama():    # nothing to offer (and no extra click) without llama.cpp
            self._speed_done = True
            return self.step_speed()
        self.step_limits() if self.outside else self.finish()

    def step_speed(self):
        """Optional (mesh/speedup.py, the same step the Linux installer runs). Nothing is downloaded or started; a 'no' changes nothing."""
        self.head("Make your AI faster (optional)", "Echo Mesh can try a draft model that guesses ahead, and test a few speed settings. The words your AI writes stay exactly the same.")
        self.text("It needs llama.cpp (llama-server) on this computer. Nothing is downloaded and nothing is started for you.", DIM)
        out = self.text("", DIM)
        r = self.row()
        def go():
            self.status_btn.config(state="disabled")
            out.config(text="Working… this can take a few minutes.")
            def work():
                import io
                buf = io.StringIO()
                try:
                    speedup.offer(self.base, lambda *_: "y", buf, models=self._speed_models)
                except Exception as e:
                    buf.write(f"It did not work here ({type(e).__name__}). Nothing was changed.\n")
                self.ui(lambda: (out.config(text=buf.getvalue().strip().split("Try it?")[-1].strip()[:900]), self.status_btn.config(text="Continue", state="normal", command=lambda: (self.step_limits() if self.outside else self.finish()))))
            threading.Thread(target=work, daemon=True).start()
        self.status_btn = self.button("Yes, try it", go, primary=True)
        self.status_btn.pack(side="left", padx=(0, 10))
        self.button("No thanks", lambda: (self.step_limits() if self.outside else self.finish())).pack(side="left")

    def step_limits(self):
        self.head("How much to share", "Echo Mesh will use at most this share of this computer's time (30% is the most outside computers may share).")
        self.scale = tk.Scale(self.body, from_=5, to=30, orient="horizontal", length=420, bg=BG, fg=FG, font=FONT, highlightthickness=0, resolution=5)
        self.scale.set(W.clamp_limit(30, True))
        self.scale.pack(pady=10)
        self.text("Your own work always comes first. You can change this or pause sharing any time from the tray icon.", DIM)
        self.button("Finish", self.finish, primary=True).pack(anchor="e", pady=14)

    def finish(self):
        limit = self.scale.get() if hasattr(self, "scale") and self.outside else None
        pol = W.load_policy(self.base)
        W.update_policy(self.base, share_limit_pct=W.clamp_limit(limit if limit is not None else pol.get("share_limit_pct", 50), self.outside), outside=self.outside)
        self.head("All set", "Echo Mesh is running. Look for its icon near the clock to see what it is doing, pause it, or change how much it shares.")
        self.result = "done"
        self.after(2500, self.destroy)                       # closes by itself

    def fail(self, msg, back):
        self.head("Couldn't connect")
        self.text(msg, "#b3372f")
        self.button("Try again", back, primary=True).pack(anchor="w", pady=12)


def icon_photo():
    """The Echo Mesh icon as a Tk image (for window title bars and the dashboard header)."""
    try:
        return tk.PhotoImage(data="".join(iconpng.PNG_B64))
    except tk.TclError:
        return None


def dashboard_state(agent, base):
    s = agent.state()
    ai = s["ai"]
    label = {"ollama": "Ollama", "openai": "OpenAI-style AI"}.get(ai["kind"], "AI") if ai.get("models") else ""
    pol = W.load_policy(base)
    return {**s, "name": socket.gethostname(), "outside": bool(pol.get("outside")), "ai": {"label": label, "host": ai["host"], "models": ai["models"]},
            "overview": agent.overview, "rtts": list(agent.rtts)}


class StatusWindow:
    """The dashboard (the look of the owner's reference picture), drawn by mesh.dashboard on a canvas, with the controls underneath."""
    def __init__(self, root, agent, base, on_pause, on_remove):
        self.agent, self.base, self.on_pause, self.on_remove = agent, base, on_pause, on_remove
        self.page = "overview"
        self.win = tk.Toplevel(root)
        self.win.title("Echo Mesh")
        self.win.configure(bg=D.BG0)
        self.win.resizable(False, False)
        self.photo = icon_photo()
        if self.photo:
            try: self.win.iconphoto(False, self.photo)
            except tk.TclError: pass
        self.canvas = tk.Canvas(self.win, width=D.W, height=D.H, bg=D.BG0, highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._click)
        bar = tk.Frame(self.win, bg=D.BG0); bar.pack(fill="x", padx=24, pady=(6, 14))
        style = dict(font=FONT, relief="flat", bd=0, padx=16, pady=8, cursor="hand2")
        self.pause_btn = tk.Button(bar, text="Pause sharing", command=self._pause, bg=D.PANEL, fg=D.TEXT, activebackground=D.EDGE, activeforeground=D.TEXT, **style)
        self.pause_btn.pack(side="left")
        cap = 30 if W.load_policy(base).get("outside") else 100
        self.cap_is_outside = bool(W.load_policy(base).get("outside"))
        tk.Label(bar, text="Share at most", font=("Segoe UI", 10), bg=D.BG0, fg=D.DIM).pack(side="left", padx=(20, 6))
        self.scale = tk.Scale(bar, from_=5, to=cap, orient="horizontal", length=170, bg=D.BG0, fg=D.TEXT, troughcolor=D.PANEL, highlightthickness=0, resolution=5, showvalue=True, bd=0)
        self.scale.set(min(cap, max(5, self.agent.local_limit)))
        self.scale.pack(side="left")
        tk.Button(bar, text="Apply", command=self._apply_limit, bg=D.BLUE, fg="white", activebackground=D.PURPLE, activeforeground="white", **style).pack(side="left", padx=10)
        tk.Button(bar, text="Remove Echo Mesh…", command=self.on_remove, bg=D.BG0, fg=D.DIM, activebackground=D.PANEL, activeforeground=D.TEXT, **style).pack(side="right")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        threading.Thread(target=self.agent.refresh_overview, daemon=True).start()      # fill in the whole-Mesh view straight away
        self.tick()

    def _click(self, e):
        for key, x, y, w, h in D.nav_regions():
            if x <= e.x <= x + w and y <= e.y <= y + h:
                self.go(key)
                return

    def go(self, page):
        self.page = page
        self.tick(once=True)

    def _apply_limit(self):
        v = self.agent.set_local_limit(self.scale.get(), self.cap_is_outside)
        W.update_policy(self.base, share_limit_pct=v)
        self.agent.note(f"Share limit set to {v:g}%")
        self.tick(once=True)

    def _pause(self):
        self.on_pause()
        self.agent.note("Sharing paused by you" if self.agent.gov.paused else "Sharing resumed")
        self.tick(once=True)

    def show(self):
        self.win.deiconify(); self.win.lift()

    def tick(self, once=False):
        try:
            self.canvas.delete("all")
            D.draw(D.TkCtx(self.canvas, self.photo), dashboard_state(self.agent, self.base), self.page)
            self.pause_btn.config(text="Resume sharing" if self.agent.gov.paused else "Pause sharing")
            if not once:
                self.win.after(2000, self.tick)
        except tk.TclError:
            pass                                          # the window was closed


def make_icon():
    import base64, io
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode("".join(iconpng.PNG_B64)))).convert("RGBA")


def run_agent(base=None):
    """The always-on part: the tray icon, the status window, and the agent doing its work."""
    pol = W.load_policy(base)
    agent = Agent(W.data_dir(base))
    if not agent.ident:
        messagebox.showinfo("Echo Mesh", "This computer isn't connected yet. Run Echo Mesh Setup again.")
        return
    root = tk.Tk(); root.withdraw()
    disp = Dispatcher(root)
    threading.Thread(target=agent.run, daemon=True).start()
    win = {}

    def toggle_pause():
        agent.gov.paused = not agent.gov.paused
        W.update_policy(base, paused=agent.gov.paused)

    def remove():
        if messagebox.askyesno("Remove Echo Mesh", "Remove Echo Mesh from this computer? It stops sharing and forgets this computer's Mesh identity. (The Mesh owner can also remove it on their side.)"):
            agent.stop.set()
            W.uninstall(base)
            root.after(200, root.destroy)       # runs on the window thread: remove() is only called from a window button

    def toggle_startup():
        on = not W.startup_enabled()
        if W.set_startup_entry(os.path.join(W.install_dir(base), W.EXE), on):
            W.update_policy(base, autostart=on)

    def open_window():
        if "w" not in win:
            win["w"] = StatusWindow(root, agent, base, toggle_pause, remove)
        win["w"].show()

    icon = None
    try:
        import pystray
        menu = pystray.Menu(pystray.MenuItem(lambda i: "Echo Mesh: " + agent.state()["connection"], None, enabled=False),
                            pystray.MenuItem("Open", lambda i, it: disp.post(open_window), default=True),
                            pystray.MenuItem(lambda i: "Resume sharing" if agent.gov.paused else "Pause sharing", lambda i, it: toggle_pause()),
                            pystray.MenuItem("Start with Windows", lambda i, it: toggle_startup(), checked=lambda it: W.startup_enabled()),
                            pystray.MenuItem("Quit", lambda i, it: (agent.stop.set(), i.stop(), disp.post(root.destroy))))
        icon = pystray.Icon("EchoMesh", make_icon(), "Echo Mesh", menu)
        icon.run_detached()
    except Exception:
        open_window()                       # no tray available: show the window instead of running invisibly
    root.mainloop()
    if icon:
        icon.stop()


def already_running():
    if sys.platform != "win32":
        return False
    import ctypes
    h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\EchoMeshAgent")
    return ctypes.windll.kernel32.GetLastError() == 183


def check(path=None):
    """Self-test used by the build: can this program start its parts? Writes a short report (a windowed program has no console)."""
    lines = [f"echo-mesh {VERSION}", f"python {sys.version.split()[0]}", f"platform {sys.platform}", "tkinter ok"]
    for mod in ("pystray", "PIL"):
        try:
            __import__(mod); lines.append(f"{mod} ok")
        except Exception as e:
            lines.append(f"{mod} MISSING ({e})")
    try:
        root = tk.Tk(); root.withdraw(); root.update(); root.destroy(); lines.append("window ok")
    except Exception as e:
        lines.append(f"window FAILED ({e})")
    out = "\n".join(lines)
    try:
        open(path or os.path.join(os.environ.get("TEMP", "."), "echomesh-check.txt"), "w").write(out)
    except OSError:
        pass
    print(out)
    return 0 if "MISSING" not in out and "FAILED" not in out else 1


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    frozen = bool(getattr(sys, "frozen", False))
    base = os.environ.get("ECHOMESH_BASE")
    if "--check" in a:
        i = a.index("--check")
        sys.exit(check(a[i + 1] if i + 1 < len(a) else None))
    try:
        if "--uninstall" in a:
            if messagebox.askyesno("Remove Echo Mesh", "Remove Echo Mesh from this computer?"):
                W.uninstall(base)
            return
        if already_running():
            return
        installed = W.is_installed_copy(base=base) if frozen else True
        have_identity = bool(Agent(W.data_dir(base)).ident)
        if "--background" in a or (installed and have_identity):
            return run_agent(base)
        address = ""
        side = os.path.join(os.path.dirname(os.path.abspath(sys.executable if frozen else __file__)), "mesh-address.txt")
        if os.path.exists(side):
            address = open(side).read().strip()
        w = Wizard(base, address, frozen)
        w.mainloop()
        if w.result != "done":
            return
        if frozen:
            dst = W.install_self(sys.executable, base, set_startup=bool(W.load_policy(base).get("autostart", True)))
            import subprocess
            subprocess.Popen([dst, "--background"], close_fds=True)
        else:
            run_agent(base)
    except Exception:
        try:
            os.makedirs(W.install_dir(base), exist_ok=True)
            open(os.path.join(W.install_dir(base), "error.log"), "a").write(time.strftime("%c") + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass
        messagebox.showerror("Echo Mesh", "Something went wrong. Details are in the error.log file in the EchoMesh folder.")


if __name__ == "__main__":
    main()
