import json, os, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import brainlet as B


class Fake(BaseHTTPRequestHandler):
    style = "ollama"
    def log_message(self, *a): pass
    def _j(self, o, code=200):
        d = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.style == "ollama" and self.path == "/api/version": return self._j({"version": "0"})
        if self.style == "ollama" and self.path == "/api/tags": return self._j({"models": [{"name": "m1"}]})
        if self.style == "openai" and self.path == "/v1/models": return self._j({"data": [{"id": "g1"}]})
        self._j({}, 404)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/generate": return self._j({"response": "hi " + body["prompt"], "eval_count": 3})
        if self.path == "/v1/chat/completions": return self._j({"choices": [{"message": {"content": "yo " + body["messages"][0]["content"]}}], "usage": {"completion_tokens": 2}})
        self._j({}, 404)


def serve(style):
    h = type("H", (Fake,), {"style": style}); s = HTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_port}"


class T(unittest.TestCase):
    def test_detects_each_kind_and_runs_a_job(self):
        for style, kind, model, out in (("ollama", "ollama", "m1", "hi x"), ("openai", "openai", "g1", "yo x")):
            s, host = serve(style)
            a = B.detect(host)
            self.assertEqual(a.kind, kind)
            b = B.Brainlet(lambda: {}, kind, host)
            ok, res, err = b.run({"type": "generate", "payload": {"model": model, "prompt": "x"}})
            self.assertTrue(ok, err); self.assertEqual(res["response"], out)
            self.assertEqual(b.status()["models"], [model]); self.assertTrue(b.status()["reachable"])
            s.shutdown()

    def test_nothing_known_answers(self):
        self.assertIsNone(B.detect("http://127.0.0.1:9"))

    def test_policy_engine_refuses_before_the_ai_is_touched(self):
        b = B.Brainlet(lambda: {"allowed_models": ["ok"], "max_prompt_chars": 5}, "ollama", "http://127.0.0.1:9")   # AI address is dead: refusal must come first
        self.assertFalse(b.run({"type": "generate", "payload": {"model": "bad", "prompt": "x"}})[0])
        self.assertIn("longer", b.run({"type": "generate", "payload": {"model": "ok", "prompt": "toolong"}})[2])
        ok, _, err = b.run({"type": "shell", "payload": {"cmd": "rm -rf /"}})
        self.assertFalse(ok); self.assertIn("not allowed", err)
        self.assertEqual(b.refused, 3)

    def test_dead_ai_is_an_error_not_a_crash_and_missing_fields_are_refused(self):
        b = B.Brainlet(lambda: {}, "ollama", "http://127.0.0.1:9")
        self.assertIn("did not answer", b.run({"type": "generate", "payload": {"model": "m", "prompt": "p"}})[2])
        self.assertIn("needs model", b.run({"type": "generate", "payload": {"model": "m"}})[2])
        self.assertFalse(B.Brainlet(lambda: {}, "partnerai", "http://x").run({"type": "generate", "payload": {"model": "m", "prompt": "p"}})[0])

    def test_a_new_kind_of_ai_is_one_registered_class(self):
        @B.register
        class Rascalish:
            kind, label = "rascalish", "Test AI"
            def __init__(self, host): pass
            @classmethod
            def probe(cls, host): return None
            def models(self): return ["r"]
            def generate(self, model, prompt, think=None): return {"response": "R:" + prompt}
        try:
            self.assertEqual(B.Brainlet(lambda: {}, "rascalish", "x").run({"type": "generate", "payload": {"model": "r", "prompt": "q"}})[1]["response"], "R:q")
        finally:
            B.ADAPTERS.pop("rascalish")

    def test_ping_reports_only_node_facts(self):
        ok, res, _ = B.Brainlet(lambda: {}, node_info=lambda: {"node": "n1"}).run({"type": "ping"})
        self.assertEqual(res, {"node": "n1"})


if __name__ == "__main__":
    unittest.main()
