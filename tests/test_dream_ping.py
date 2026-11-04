"""Echo → Dream Bridge 'someone is talking' ping (core/sched.py). It must never slow a conversation."""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import sched


def _reset(monkeypatch, url):
    monkeypatch.setattr(sched, "_dream_last", 0.0)
    monkeypatch.setattr(sched, "DREAM_PING_URL", url)


def test_ping_reaches_bridge_and_is_throttled(monkeypatch):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _reset(monkeypatch, f"http://127.0.0.1:{srv.server_port}/conversation/ping")
    assert sched.notify_dream_bridge() is True
    assert sched.notify_dream_bridge() is False          # within 20 s: no second ping
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    srv.shutdown()
    assert got == ["/conversation/ping"]


def test_turn_started_never_waits_for_bridge(monkeypatch):
    _reset(monkeypatch, "http://10.255.255.1:9/never")   # unroutable: would hang if called inline
    s = sched.Scheduler(memory_probe=lambda: 50)
    t = time.perf_counter()
    s.turn_started()
    assert time.perf_counter() - t < 0.05


def test_can_be_switched_off(monkeypatch):
    _reset(monkeypatch, "http://127.0.0.1:9/x")
    monkeypatch.setenv("ECHO_DREAM_BRIDGE", "0")
    assert sched.notify_dream_bridge() is False
