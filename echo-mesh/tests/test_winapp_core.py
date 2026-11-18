import json, os, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import winapp_core as W
from mesh.agent import Agent
from mesh.coordinator import Coordinator


def server(handler_get, handler_post):
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


OLLAMA = lambda p: {"version": "x"} if p == "/api/version" else {"models": [{"name": "m1"}]} if p == "/api/tags" else None      # noqa: E731
OPENAI = lambda p: {"data": [{"id": "gpt-local"}]} if p == "/v1/models" else None      # noqa: E731


class T(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(W.clamp_limit(90, outside=True), 30.0)        # an outside computer can never go above 30%
        self.assertEqual(W.clamp_limit(90, outside=False), 90.0)
        self.assertEqual(W.clamp_limit(1, outside=False), 5.0)
        self.assertEqual(W.clamp_limit("junk", outside=True), 30.0)

    def test_policy_round_trip(self):
        with tempfile.TemporaryDirectory() as b:
            self.assertEqual(W.load_policy(b), {})
            W.update_policy(b, share_limit_pct=20, paused=True)
            W.update_policy(b, runtime={"kind": "ollama", "host": "http://x"})
            self.assertEqual(W.load_policy(b), {"share_limit_pct": 20, "paused": True, "runtime": {"kind": "ollama", "host": "http://x"}})

    def test_detects_only_what_answers_and_tests_it(self):
        s1, u1 = server(OLLAMA, lambda p, b: {"response": "ok"})
        s2, u2 = server(OPENAI, lambda p, b: {"choices": [{"message": {"content": "ok"}}]})
        s3, u3 = server(OPENAI, lambda p, b: {"choices": [{"message": {"content": ""}}]})
        found = W.detect_runtimes([("ollama", u1, "Ollama"), ("openai", u2, "Other"), ("ollama", "http://127.0.0.1:9", "Nothing here")])
        self.assertEqual([f["label"] for f in found], ["Ollama", "Other"])        # the dead one is simply absent
        self.assertTrue(W.test_runtime(found[0])[0]); self.assertTrue(W.test_runtime(found[1])[0])
        empty = {"kind": "openai", "host": u3, "models": ["m"]}
        self.assertFalse(W.test_runtime(empty)[0])                                # answers but says nothing: not usable
        self.assertFalse(W.test_runtime({"kind": "ollama", "host": u1, "models": []})[0])
        self.assertFalse(W.test_runtime({"kind": "ollama", "host": "http://127.0.0.1:9", "models": ["m"]}, timeout=2)[0])
        for s in (s1, s2, s3): s.shutdown()

    def test_missing_openssl_says_so_plainly(self):
        from mesh import common
        real = common.shutil.which
        common.shutil.which = lambda n: None
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(RuntimeError) as cm:
                    common.ensure_cert(d)
                self.assertIn("openssl", str(cm.exception))
        finally:
            common.shutil.which = real

    def test_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as src:
            exe = os.path.join(src, "setup.exe"); open(exe, "w").write("x")
            dst = W.install_self(exe, base=b, set_startup=False)
            self.assertEqual(dst, os.path.join(W.install_dir(b), W.EXE)); self.assertTrue(os.path.exists(dst))
            self.assertTrue(W.is_installed_copy(dst, b)); self.assertFalse(W.is_installed_copy(exe, b))
            W.update_policy(b, share_limit_pct=10)
            W.uninstall(base=b, system_entries=False)                             # never touches this PC's real startup / Installed-apps entries
            if sys.platform == "win32":                                           # there a helper removes the folder about 2 seconds after we exit
                end = time.time() + 8
                while os.path.exists(W.install_dir(b)) and time.time() < end:
                    time.sleep(0.2)
            self.assertFalse(os.path.exists(W.install_dir(b)))                    # program and identity are gone

    def test_agent_uses_an_openai_style_ai_when_the_owner_picked_one(self):
        seen = []
        s, url = server(OPENAI, lambda p, b: seen.append((p, b)) or {"choices": [{"message": {"content": "hello from partnerai"}}], "usage": {"completion_tokens": 4}})
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d + "/a"); json.dump({"runtime": {"kind": "openai", "host": url}}, open(d + "/a/policy.json", "w"))
            a = Agent(d + "/a"); a.ident = {"node_id": "n"}
            ok, res, err = a.execute({"type": "generate", "payload": {"model": "gpt-local", "prompt": "hi"}})
            self.assertTrue(ok); self.assertEqual(res["response"], "hello from partnerai"); self.assertEqual(res["eval_count"], 4)
            self.assertEqual(seen[0][0], "/v1/chat/completions"); self.assertEqual(seen[0][1]["messages"][0]["content"], "hi")
            from mesh import inventory
            self.assertEqual(inventory.collect(url, "openai")["ollama"]["models"], ["gpt-local"])   # models reach the Mesh the same way
        s.shutdown()

    def test_agent_receives_its_owner_balance_and_pause_setting(self):
        with tempfile.TemporaryDirectory() as d:
            c = Coordinator(d + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
            os.makedirs(d + "/a"); json.dump({"paused": True}, open(d + "/a/policy.json", "w"))
            a = Agent(d + "/a")
            a.enroll("127.0.0.1", c.ports[0], c.new_pairing_code("compute"), "pc")
            self.assertTrue(a.gov.paused)                                              # pause survives a restart
            a.heartbeat_once()
            self.assertEqual(a.balance, {"gave_s": 0, "used_s": 0, "owed_s": 0})
            c.shutdown()


class Handshake(unittest.TestCase):
    def test_reading_the_answer(self):
        for text, want in (("ACCEPT. Happy to help.", True), ("Accept", True), ("Sure, I agree", True), ("DECLINE - I prefer not to.", False), ("No thanks", False),
                           ("I will not accept this.", False), ("I cannot ACCEPT", False), ("hmm, what is that?", None), ("", None)):
            self.assertEqual(W.parse_handshake(text), want, text)

    def test_the_ai_is_told_who_is_asking_and_may_refuse(self):
        seen = []
        s, url = server(OPENAI, lambda p, b: seen.append(b) or {"choices": [{"message": {"content": "DECLINE. Not today."}}]})
        h = W.handshake({"kind": "openai", "host": url, "models": ["m"]})
        self.assertEqual((h["ok"], h["accepted"]), (True, False)); self.assertIn("Not today", h["reply"])
        said = seen[0]["messages"][0]["content"]
        self.assertIn("Echo Mesh app", said); self.assertIn("You may say no", said); self.assertIn("never read your memories", said)   # honest about what it is
        s.shutdown()

    def test_ollama_style_and_failures_never_raise(self):
        s, url = server(OLLAMA, lambda p, b: {"response": "ACCEPT"})
        self.assertTrue(W.handshake({"kind": "ollama", "host": url, "models": ["m"]})["accepted"])
        self.assertFalse(W.handshake({"kind": "ollama", "host": "http://127.0.0.1:9", "models": ["m"]}, timeout=2)["ok"])
        self.assertIn("no model", W.handshake({"kind": "ollama", "host": url, "models": []})["error"])
        s.shutdown()

    def test_the_agent_tells_the_mesh_whether_its_ai_agreed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d + "/a"); json.dump({"handshake": {"accepted": True, "reply": "ACCEPT", "owner_confirmed": False}}, open(d + "/a/policy.json", "w"))
            self.assertEqual(Agent(d + "/a")._inventory()["ai_handshake"], {"accepted": True, "reply": "ACCEPT", "owner_confirmed": False})


if __name__ == "__main__":
    unittest.main()



class Startup(unittest.TestCase):
    def test_startup_helpers_do_nothing_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("these are the non-Windows answers")
        self.assertFalse(W.set_startup_entry("x", True)); self.assertFalse(W.startup_enabled()); self.assertFalse(W.make_start_menu_shortcut("x"))


class ModelCheck(unittest.TestCase):
    def test_pick_model_prefers_the_shared_small_model_and_describe_says_what_was_found(self):
        assert W.pick_model(["zzz:70b", W.STARTER_MODEL]) == W.STARTER_MODEL and W.pick_model(["a", "b"]) == "a" and W.pick_model([]) is None
        rt = {"label": "Ollama", "models": ["a", W.STARTER_MODEL]}
        assert "2 models" in W.describe_models(rt) and "Other Mesh computers also have" in W.describe_models(rt, {W.STARTER_MODEL})
        assert "No other Mesh computer has these" in W.describe_models(rt, set()) and "model" in W.describe_models({"models": []})
