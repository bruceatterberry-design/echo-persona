"""Drives the real Echo Mesh wizard (Tk) against a real coordinator. Needs tkinter and a screen; skipped otherwise.
Run with a Python that has tkinter, e.g.  ~/Sweetie/.venv/bin/python -m unittest tests.test_winapp_gui"""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import tkinter as tk
    HAVE_TK = bool(os.environ.get("DISPLAY") or sys.platform in ("darwin", "win32"))
except Exception:
    HAVE_TK = False


OPENAI_STYLE = lambda p: {"data": [{"id": "m"}]} if p == "/v1/models" else None      # noqa: E731


def server(handler_get, handler_post):
    import json, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, o):
            d = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
        def do_GET(self):
            r = handler_get(self.path)
            self._j(r) if r is not None else (self.send_response(404), self.end_headers())
        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); self._j(handler_post(self.path, b))
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def labels(w):
    out = []
    def walk(x):
        for c in x.winfo_children():
            try:
                t = c.cget("text")
                if t: out.append(str(t))
            except tk.TclError:
                pass
            walk(c)
    walk(w.body)
    return " | ".join(out)


def pump(w, cond, secs=15):
    end = time.time() + secs
    while time.time() < end:
        w.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(HAVE_TK, "needs tkinter and a screen")
class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ONE window for the whole class: creating and destroying many Tk roots in one process crashes some Tk builds (the real app makes one).
        from mesh import winapp, winapp_core as W
        from mesh import speedup
        cls.winapp, cls.W, cls.speedup, cls.real_offer, cls.real_find = winapp, W, speedup, staticmethod(speedup.offer), staticmethod(speedup.find_llama)
        cls.tmp0 = tempfile.TemporaryDirectory()
        cls.originals = {n: getattr(W, n) for n in ("detect_runtimes", "test_runtime", "handshake", "pull_model", "probe")}
        cls.w = winapp.Wizard(base=cls.tmp0.name + "/b0", splash=False)       # the full-screen splash is drawn by tests/test_splash.py, not shown here
        cls.real_destroy = cls.w.destroy
        cls.closed = []
        cls.w.destroy = lambda: cls.closed.append(1)       # the real wizard closes itself at the end; here we just count it

    @classmethod
    def tearDownClass(cls):
        cls.w.agent.stop.set(); cls.real_destroy(); cls.tmp0.cleanup()

    REAL_TEST = staticmethod(__import__("mesh.winapp_core", fromlist=["x"]).test_runtime)

    def setUp(self):
        from mesh.coordinator import Coordinator
        from mesh.agent import Agent
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        self.winapp.discover = lambda timeout=3: ("127.0.0.1", self.c.ports[0], None)
        for n, f in self.originals.items():
            setattr(self.W, n, f)
        self.speedup.offer, self.speedup.find_llama = self.real_offer, self.real_find
        self.w._speed_models, self.w._speed_done = None, False      # the optional speed screen starts fresh in every test
        self.W.find_ollama_exe = lambda: None                       # tests start from "Ollama is not installed"                                   # every test starts with the real functions, not a leftover fake
        self.W.detect_runtimes = lambda *a, **k: []
        self.w.base = self.tmp.name + "/b"
        self.w.agent.stop.set()
        self.w.agent = Agent(self.W.data_dir(self.w.base))      # a fresh, not-yet-enrolled agent for each test
        self.w.address, self.w.read_to_end = "", False
        self.w.outside = False
        self.w.ai_gen = getattr(self.w, "ai_gen", 0) + 1       # cancel any scanning left from an earlier test
        self.closed.clear()
        for a in ("scale",):
            if hasattr(self.w, a): delattr(self.w, a)
        self.w.step_welcome()

    def tearDown(self):
        self.w.agent.stop.set()
        self.c.shutdown(); self.tmp.cleanup()

    def test_home_join_number_matches_and_moves_on_after_approval(self):
        self.w.start("home")
        self.assertTrue(pump(self.w, lambda: self.c.pending_requests()))
        req = self.c.pending_requests()[0]
        self.assertTrue(pump(self.w, lambda: req["confirm"] in labels(self.w)))        # the number on the PC equals the Mac's number
        self.assertIn("Waiting for you to approve", labels(self.w))
        self.c.decide_request(req["id"], True)
        self.assertTrue(pump(self.w, lambda: "The AI on this computer" in labels(self.w)))
        self.assertTrue(self.w.agent.ident and self.w.agent.heartbeat_once()[0] == 200)  # it is a real working node

    def test_terms_must_be_read_to_the_end_and_ticked_before_configuration(self):
        self.w.step_terms(); self.w.update()
        self.assertEqual(str(self.w.next_btn.cget("state")), "disabled"); self.assertEqual(str(self.w.c1.cget("state")), "disabled")
        self.assertIsNone(self.w.c2)                                                  # the terms have one box, the participation agreement has two
        self.w.after_terms(); self.w.update()
        self.assertNotIn("Choose this computer", labels(self.w))                    # pressing Continue early does nothing
        self.w._scrolled(1.0); self.w.c1.invoke(); self.w.update()
        self.assertEqual(str(self.w.next_btn.cget("state")), "normal")
        self.w.after_terms(); self.w.update()
        self.assertIn("Choose this computer", labels(self.w))
        self.assertEqual(self.W.load_policy(self.w.base)["terms_accepted"]["version"], self.winapp.TERMS_VERSION)

    def test_configuration_defaults_to_worker_and_is_reported_to_the_mesh(self):
        self.w.step_config(); self.w.update()
        self.assertEqual(self.w.cfg.get(), "worker")                                  # the living-room PC: recommended choice already selected
        self.w.after_config(); self.assertEqual(self.w.role, "worker")
        self.assertEqual(self.W.load_policy(self.w.base)["role"], "worker")
        self.w.start("home")
        self.assertTrue(pump(self.w, lambda: self.c.pending_requests()))
        req = self.c.pending_requests()[0]
        self.assertEqual(req["role"], "worker")                                       # the owner sees what it asked to be before approving
        self.c.decide_request(req["id"], True)
        self.assertTrue(pump(self.w, lambda: "The AI on this computer" in labels(self.w)))
        self.assertEqual(self.c.overview({"id": "x", "lan": 1, "owner": "owner"})["nodes"][0]["role"], "worker")

    def test_start_on_boot_is_asked_ticked_by_default_and_remembered(self):
        self.w.step_config(); self.w.update()
        self.assertIn("Start Echo Mesh automatically when this computer starts", labels(self.w)); self.assertTrue(self.w.autostart.get())
        self.w.autostart.set(False); self.w.after_config()
        self.assertIs(self.W.load_policy(self.w.base)["autostart"], False)
        self.w.step_config(); self.w.after_config()
        self.assertIs(self.W.load_policy(self.w.base)["autostart"], True)

    def test_a_computer_cannot_ask_to_be_primary(self):
        self.W.save_policy({"role": "primary"}, self.w.base)
        self.w.agent = type(self.w.agent)(self.W.data_dir(self.w.base))
        self.assertNotIn("role", self.w.agent._inventory())                           # anything but worker/peer is not even sent

    def test_denied_request_shows_a_plain_message(self):
        self.w.start("home")
        self.assertTrue(pump(self.w, lambda: self.c.pending_requests()))
        self.c.decide_request(self.c.pending_requests()[0]["id"], False)
        self.assertTrue(pump(self.w, lambda: "denied" in labels(self.w)))

    def test_agreement_gates_both_boxes_until_read_to_the_end(self):
        self.w.start("invite")
        self.w.update()
        self.assertEqual(str(self.w.next_btn.cget("state")), "disabled")
        self.assertEqual(str(self.w.c1.cget("state")), "disabled")                    # boxes are locked until the end is reached
        self.w.step_code(); self.assertIn("participation agreement", labels(self.w).lower())   # cannot skip ahead
        self.w._scrolled(1.0)
        self.assertEqual(str(self.w.c2.cget("state")), "normal")
        self.w.v1.set(True); self.w._boxes(); self.assertEqual(str(self.w.next_btn.cget("state")), "disabled")   # one box is not enough
        self.w.v2.set(True); self.w._boxes(); self.assertEqual(str(self.w.next_btn.cget("state")), "normal")
        self.w.v1.set(False); self.w._boxes(); self.assertEqual(str(self.w.next_btn.cget("state")), "disabled")

    def test_invite_flow_waits_for_approval_then_caps_sharing_at_30(self):
        self.w.address = f"127.0.0.1:{self.c.ports[0]}"
        self.w.start("invite"); self.w.read_to_end = True; self.w.v1.set(True); self.w.v2.set(True)
        self.w.step_code()
        code = self.c.new_pairing_code("compute", owner="sam", lan=False)
        self.w.code.insert(0, code); self.w.connect()
        self.assertTrue(pump(self.w, lambda: "Waiting for the Mesh owner" in labels(self.w)))
        nid = self.w.agent.ident["node_id"]
        self.assertEqual(self.c.store.one("SELECT status, owner, lan FROM nodes WHERE id=?", (nid,)), {"status": "pending", "owner": "sam", "lan": 0})
        self.c.approve(nid)
        self.assertTrue(pump(self.w, lambda: "The AI on this computer" in labels(self.w), 20))
        self.w.step_limits()
        self.assertEqual(int(float(self.w.scale.cget("to"))), 30)                      # the slider cannot go past 30
        self.w.scale.set(30); self.w.finish()
        pol = self.W.load_policy(self.w.base)
        self.assertEqual((pol["share_limit_pct"], pol["outside"]), (30.0, True))

    def test_wrong_code_and_bad_input_are_plain_messages(self):
        self.w.address = f"127.0.0.1:{self.c.ports[0]}"
        self.w.start("invite"); self.w.read_to_end = True; self.w.v1.set(True); self.w.v2.set(True); self.w.step_code()
        self.w.code.insert(0, "12"); self.w.connect()
        self.assertIn("4 digits", labels(self.w))
        real = self.c.new_pairing_code("compute", lan=False)
        wrong = "0000" if real != "0000" else "1111"
        self.w.code.delete(0, "end"); self.w.code.insert(0, wrong); self.w.connect()
        self.assertTrue(pump(self.w, lambda: "not accepted" in labels(self.w)))        # plain message, and the button is usable again
        self.assertEqual(str(self.w.go.cget("state")), "normal")

    def runtime(self, models=("m",), label="a partner AI's AI", kind="openai", host="http://127.0.0.1:1"):
        return {"kind": kind, "host": host, "label": label, "models": list(models)}

    def test_home_computer_with_one_ai_needs_no_clicking_and_closes_by_itself(self):
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("m",), "Ollama", "ollama")]
        tested = []
        self.W.test_runtime = lambda rt, timeout=90: (tested.append(rt), (True, "ok"))[1]
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w)))                 # found, tested, finished, with no clicks
        self.assertEqual(tested[0]["label"], "Ollama")
        self.assertEqual(self.W.load_policy(self.w.base)["runtime"], {"kind": "ollama", "host": "http://127.0.0.1:1"})
        self.assertEqual(self.W.load_policy(self.w.base)["share_limit_pct"], 50.0)         # a sensible default, changeable from the tray
        self.assertTrue(pump(self.w, lambda: self.closed, 6))                              # and the window closes itself

    def test_the_models_found_are_listed_before_the_test(self):
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("a:1b", "b:3b"), "Ollama", "ollama")]
        self.W.test_runtime = lambda rt, timeout=90: (True, "ok")
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "2 models: a:1b, b:3b" in labels(self.w) or "All set" in labels(self.w)))
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w)))

    def test_installed_but_stopped_ollama_offers_to_start_it_not_to_download_it(self):
        started = []
        self.W.detect_runtimes = lambda *a, **k: []
        self.W.find_ollama_exe = lambda: "C:/fake/ollama.exe"
        self.W.start_ollama = lambda exe: (started.append(exe), True)[1]
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "installed on this computer, but it isn't running" in labels(self.w)))
        self.assertIn("Start Ollama", labels(self.w)); self.assertNotIn("Get Ollama (opens the download page)", labels(self.w))
        def find(x):
            for c in x.winfo_children():
                if c.winfo_class() == "Button" and c.cget("text") == "Start Ollama": return c
                r = find(c)
                if r: return r
        find(self.w.body).invoke(); self.assertEqual(started, ["C:/fake/ollama.exe"])

    def test_no_ai_yet_waits_and_carries_on_by_itself_once_ollama_appears(self):
        calls = []
        def scan(*a, **k):
            calls.append(1)
            return [] if len(calls) < 2 else [self.runtime(("m",), "Ollama", "ollama")]
        self.W.detect_runtimes = scan
        self.W.test_runtime = lambda rt, timeout=90: (True, "ok")
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "Get Ollama" in labels(self.w)))
        self.assertIn("Skip for now", labels(self.w))
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w), 15))             # it noticed Ollama by itself

    def test_ollama_with_no_model_offers_a_starter_download_with_progress(self):
        pulled = []
        state = {"models": []}
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(tuple(state["models"]), "Ollama", "ollama")]
        def pull(host, model=None, on_progress=None, timeout=0):
            on_progress(1_000_000_000, 2_000_000_000, "pulling"); pulled.append(model or self.W.STARTER_MODEL); state["models"].append("starter")
            return True, "Downloaded."
        self.W.pull_model = pull
        self.W.test_runtime = lambda rt, timeout=90: (True, "ok")
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "no AI model yet" in labels(self.w)))
        self.assertIn("about 2 GB", labels(self.w))
        self.w.get_btn.invoke()                                                            # the person says yes to the download
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w), 15))
        self.assertEqual(pulled, [self.W.STARTER_MODEL])

    def test_outside_computer_is_never_pushed_to_install_ollama(self):
        self.w.outside = True
        self.W.detect_runtimes = lambda *a, **k: []
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "couldn't find your AI" in labels(self.w)))
        self.assertNotIn("Ollama (opens", labels(self.w)); self.assertNotIn("starter", labels(self.w).lower())
        self.assertIn("Skip for now", labels(self.w))

    def test_outside_manual_address_then_the_ai_is_introduced_and_agrees(self):
        self.w.outside = True
        self.W.detect_runtimes = lambda *a, **k: []
        s, url = server(OPENAI_STYLE, lambda p, b: {"choices": [{"message": {"content": "ACCEPT. Glad to help."}}]})
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "couldn't find your AI" in labels(self.w)))
        self.w.man_addr.delete(0, "end"); self.w.man_addr.insert(0, "http://127.0.0.1:9"); self.w.use_manual()
        self.assertIn("Nothing answered", labels(self.w))                                   # a wrong address is a plain message
        self.w.man_addr.delete(0, "end"); self.w.man_addr.insert(0, url); self.w.use_manual()
        self.assertTrue(pump(self.w, lambda: "Your AI agreed to connect" in labels(self.w)))
        self.assertIn("ACCEPT. Glad to help.", labels(self.w))                              # a partner sees the AI's own words
        self.assertNotIn("runtime", self.W.load_policy(self.w.base))                         # nothing is saved until he presses Connect
        self.find_button("Connect it").invoke()
        self.assertTrue(pump(self.w, lambda: "How much to share" in labels(self.w)))        # outside computers still choose their limit
        pol = self.W.load_policy(self.w.base)
        self.assertEqual(pol["runtime"], {"kind": "openai", "host": url}); self.assertTrue(pol["handshake"]["accepted"])
        s.shutdown()

    def find_button(self, text):
        out = []
        def walk(x):
            for c in x.winfo_children():
                try:
                    if c.cget("text") == text and c.winfo_class() == "Button": out.append(c)
                except tk.TclError: pass
                walk(c)
        walk(self.w.body)
        return out[0]

    def test_an_ai_that_says_no_is_not_connected(self):
        self.w.outside = True
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("a",), "a partner AI's AI")]
        self.W.handshake = lambda rt, timeout=120: {"ok": True, "accepted": False, "reply": "DECLINE. I'd rather not.", "error": ""}
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "a partner AI's AI" in labels(self.w)))
        self.w.choice.set(0); self.w.use.invoke()
        self.assertTrue(pump(self.w, lambda: "said no" in labels(self.w)))
        self.assertIn("I'd rather not", labels(self.w))
        self.assertNotIn("Connect it", labels(self.w)); self.assertNotIn("Connect anyway", labels(self.w))
        self.assertNotIn("runtime", self.W.load_policy(self.w.base))

    def test_an_unclear_answer_needs_the_owner_to_decide(self):
        self.w.outside = True
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("a",), "a partner AI's AI")]
        self.W.handshake = lambda rt, timeout=120: {"ok": True, "accepted": None, "reply": "Hmm, what is that?", "error": ""}
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "a partner AI's AI" in labels(self.w)))
        self.w.choice.set(0); self.w.use.invoke()
        self.assertTrue(pump(self.w, lambda: "You decide" in labels(self.w)))
        self.find_button("Connect anyway").invoke()
        self.assertTrue(pump(self.w, lambda: "How much to share" in labels(self.w)))
        h = self.W.load_policy(self.w.base)["handshake"]
        self.assertEqual((h["accepted"], h["owner_confirmed"]), (None, True))                # recorded as the owner's decision, not the AI's

    def test_a_home_computer_is_not_introduced_just_tested(self):
        self.speedup.find_llama = lambda: "/x/llama-server"
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("m",), "Ollama", "ollama")]
        intro = []
        self.W.handshake = lambda rt, timeout=120: intro.append(1)
        self.W.test_runtime = lambda rt, timeout=90: (True, "ok")
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "Make your AI faster (optional)" in labels(self.w)))     # the optional speed-up screen, offered for the owner's own Ollama
        self.find_button("No thanks").invoke()
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w)))
        self.assertEqual(intro, [])
        self.assertEqual(self.speedup.load(self.w.base), {})                                         # saying no changes nothing

    def test_the_speed_screen_runs_the_shared_step_and_continues(self):
        seen = []
        self.speedup.find_llama = lambda: "/x/llama-server"
        self.W.detect_runtimes = lambda *a, **k: [self.runtime(("m",), "Ollama", "ollama")]
        self.W.test_runtime = lambda rt, timeout=90: (True, "ok")
        self.speedup.offer = lambda d, ask, out, models=None, **k: (seen.append((ask(), models)), out.write("Try it? Done: all fine.\n"))[0]
        self.w.step_ai()
        self.assertTrue(pump(self.w, lambda: "Make your AI faster (optional)" in labels(self.w)))
        self.find_button("Yes, try it").invoke()
        self.assertTrue(pump(self.w, lambda: "Done: all fine." in labels(self.w)))
        self.assertEqual(seen, [("y", ["m"])])
        self.find_button("Continue").invoke()
        self.assertTrue(pump(self.w, lambda: "All set" in labels(self.w)))

    def test_a_prefilled_address_is_never_asked_for(self):
        self.w.address = f"127.0.0.1:{self.c.ports[0]}"
        self.w.start("invite"); self.w.read_to_end = True; self.w.v1.set(True); self.w.v2.set(True); self.w.step_code()
        self.assertIsNone(self.w.addr); self.assertNotIn("Mesh address", labels(self.w))
        self.assertIn("4-digit pairing code", labels(self.w))
        code = self.c.new_pairing_code("compute", owner="sam", lan=False)
        self.w.code.insert(0, code); self.w.connect()
        self.assertTrue(pump(self.w, lambda: "Waiting for the Mesh owner" in labels(self.w)))        # connected with only the code typed

    def test_running_it_again_after_joining_resumes_instead_of_asking_again(self):
        from mesh.agent import Agent
        a = Agent(self.W.data_dir(self.w.base)); a.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute"), "pc")
        self.w.agent = a
        self.W.detect_runtimes = lambda *a, **k: []
        self.w.step_welcome()
        threading_start = self.w._resume_check
        import threading; threading.Thread(target=threading_start, daemon=True).start()
        self.assertTrue(pump(self.w, lambda: "Welcome back" in labels(self.w)))
        self.assertTrue(pump(self.w, lambda: "The AI on this computer" in labels(self.w), 6))
        self.assertEqual(len(self.c.store.q("SELECT id FROM nodes")), 1)                   # no second entry was made
        self.assertEqual(self.c.pending_requests(), [])

    def test_unreachable_outside_address_explains_tailscale(self):
        self.w.address = "127.0.0.1:9"
        self.w.start("invite"); self.w.read_to_end = True; self.w.v1.set(True); self.w.v2.set(True); self.w.step_code()
        self.w.code.insert(0, "1234"); self.w.connect()
        self.assertTrue(pump(self.w, lambda: "Tailscale" in labels(self.w)))
        self.assertIn("Get Tailscale", labels(self.w))

    def test_dashboard_window_shows_the_real_mesh_and_its_controls_work(self):
        from mesh.agent import Agent
        a = Agent(self.tmp.name + "/s"); a.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute"), "pc"); a.heartbeat_once(True)
        b = Agent(self.tmp.name + "/s2"); b.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute"), "other-pc"); b.heartbeat_once(True)
        self.assertEqual(len(a.refresh_overview()["nodes"]), 2)
        sw = self.winapp.StatusWindow(self.w, a, self.w.base, lambda: setattr(a.gov, "paused", not a.gov.paused), lambda: None)
        self.w.update()
        def shown(): return " | ".join(str(sw.canvas.itemcget(i, "text")) for i in sw.canvas.find_all() if sw.canvas.type(i) == "text")
        self.assertGreaterEqual(len(sw.canvas.find_all()), 100)                             # it really drew the whole dashboard
        for word in ("NODES ONLINE", "ECHO MESH NETWORK", "other-pc", "Mesh Healthy", "ACTIVITY FEED", "NETWORK STATISTICS"): self.assertIn(word, shown())
        sw._click(type("E", (), {"x": 80, "y": 200})())                                    # click "Nodes" in the left menu
        self.assertEqual(sw.page, "nodes"); self.assertIn("Share limit / using", shown())
        sw._click(type("E", (), {"x": 80, "y": 112 + 5 * 52 + 10})())                      # click "Settings"
        self.assertEqual(sw.page, "settings")
        sw._click(type("E", (), {"x": 600, "y": 400})())                                   # a click elsewhere changes nothing
        self.assertEqual(sw.page, "settings")
        sw.go("overview")
        sw._pause(); self.assertEqual(sw.pause_btn.cget("text"), "Resume sharing"); self.assertTrue(a.gov.paused)
        sw.scale.set(35); sw._apply_limit()
        self.assertEqual(a.local_limit, 35.0); self.assertEqual(a.gov.limit, 35.0)
        self.assertEqual(self.W.load_policy(self.w.base)["share_limit_pct"], 35.0)         # kept for next time

    def test_an_outside_computer_cannot_raise_its_limit_past_30_in_the_dashboard(self):
        from mesh.agent import Agent
        self.W.update_policy(self.w.base, outside=True)
        a = Agent(self.tmp.name + "/o"); a.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute", lan=False), "pc"); self.c.approve(a.ident["node_id"])
        sw = self.winapp.StatusWindow(self.w, a, self.w.base, lambda: None, lambda: None)
        self.assertEqual(int(float(sw.scale.cget("to"))), 30)
        self.assertEqual(a.set_local_limit(90, outside=True), 30.0)

if __name__ == "__main__":
    unittest.main()
