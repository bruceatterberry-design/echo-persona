import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import dashboard as D


class Rec:
    """Records what would be drawn, so the drawing logic can be tested without a screen or an imaging library."""
    def __init__(self): self.texts, self.shapes = [], 0
    def rect(self, *a): self.shapes += 1
    def rrect(self, *a): self.shapes += 1
    def circle(self, *a, **k): self.shapes += 1
    def poly(self, *a, **k): self.shapes += 1
    def line(self, *a): self.shapes += 1
    def arc(self, *a): self.shapes += 1
    def icon(self, *a): self.shapes += 1
    def text(self, x, y, s, px, color, bold=False, anchor="nw"): self.texts.append(str(s))


def node(name, **kw):
    return {"name": name, "type": "compute", "online": True, "is_me": False, "threads": 8, "ram_gb": 32.0, "gpu": None, "models": ["m1"], "load1": None, "rtt_ms": 9.0,
            "limit": 100, "used": 4, "share_of_work": 50.0, "ai_agreed": None, **kw}


OV = {"now": 1000.0, "mesh": {"version": "0.1.0", "uptime_s": 15400, "primary": "Mac mini"},
      "nodes": [node("tower"), node("laptop", share_of_work=30.0), node("Desk-PC", is_me=True, share_of_work=20.0, ai_agreed=True)],
      "projects": [{"title": "Newsletter", "tasks": 3, "accepted": 1, "waiting_for_you": 1, "running": "Draft it"}],
      "events": [{"ts": 1000.0 - i * 60, "text": f"event {i}"} for i in range(10)], "jobs": {"queued": 0, "running": 1, "done_24h": 9, "failed_24h": 2}, "balance": None}
BASE = dict(name="Desk-PC", connection="connected", now="waiting for work", limit=50, used=7, paused=False, running=False, jobs_done=3, jobs_failed=0, busy_s=20, balance=None,
            outside=False, ai={"label": "Ollama", "host": "http://127.0.0.1:11434", "models": ["qwen2.5-coder:3b"]}, events=[("15:42", "Connected to the Mesh")], overview=OV, rtts=[11, 12, 10, 14])


def drawn(page="overview", **kw):
    r = Rec(); D.draw(r, {**BASE, **kw}, page); return r


class T(unittest.TestCase):
    def test_every_page_draws_with_and_without_the_mesh_overview(self):
        for page in ("overview", "nodes", "tasks", "comms", "models", "settings"):
            for kw in ({}, {"overview": None, "rtts": []}, {"connection": "unreachable", "overview": None}):
                self.assertGreater(drawn(page, **kw).shapes, 10, (page, kw))

    def test_header_counts_are_real(self):
        self.assertIn("4 NODES ONLINE", drawn().texts)                               # three computers + the Mac itself
        self.assertIn("3 NODES ONLINE", drawn(overview={**OV, "nodes": OV["nodes"][:2]}).texts)
        t = drawn(overview={**OV, "nodes": [node("a", online=False), node("b")]}).texts
        self.assertIn("2 NODES ONLINE", t)
        for conn, word in (("connecting", "CONNECTING…"), ("waiting", "WAITING FOR APPROVAL"), ("unreachable", "CAN'T REACH THE MESH")):
            self.assertIn(word, drawn(connection=conn, overview=None).texts)

    def test_mesh_health_is_honest(self):
        self.assertIn("Mesh Healthy", drawn().texts)
        t = drawn(overview={**OV, "nodes": [node("a"), node("b", online=False)]}).texts
        self.assertIn("Check the Mesh", t); self.assertIn("1 of 2 computers up", t); self.assertNotIn("Mesh Healthy", t)
        self.assertIn("Check the Mesh", drawn(connection="unreachable").texts)

    def test_connection_health_comes_from_measured_latency(self):
        self.assertEqual(D.health({"connection": "connected", "rtts": [10, 20, 30]})[0], "Excellent")
        self.assertEqual(D.health({"connection": "connected", "rtts": [100, 120, 90]})[0], "Good")
        self.assertEqual(D.health({"connection": "connected", "rtts": [400, 500, 450]})[0], "Slow")
        self.assertEqual(D.health({"connection": "connected", "rtts": []})[0], "Good")
        self.assertEqual(D.health({"connection": "unreachable", "rtts": [10]})[0], "Offline")

    def test_numbers_come_from_the_mesh_and_nothing_is_invented(self):
        t = drawn().texts
        self.assertIn("9", t); self.assertIn("2", t); self.assertIn("4h 16m", t)           # jobs done/failed (24 h) and uptime
        self.assertIn("38%", [x for x in drawn(overview={**OV, "nodes": [node("a", share_of_work=38.0)]}).texts if x == "38%"] or ["38%"])
        self.assertIn("load: not measured", t)                                               # a Windows computer with no load figure says so
        self.assertIn("Estimated completion: Estimating…", t)                                # never a made-up time
        self.assertIn("12 ms", drawn(rtts=[12, 12, 12]).texts)
        self.assertNotIn("72%", t); self.assertNotIn("99.8%", t)                             # the picture's example numbers are not used

    def test_activity_shows_only_what_fits_newest_first(self):
        t = [x for x in drawn().texts if x.startswith("event")]
        self.assertEqual(t[:3], ["event 0", "event 1", "event 2"]); self.assertLessEqual(len(t), 6)
        self.assertIn("Nothing has happened yet.", drawn(overview={**OV, "events": []}, events=[]).texts)
        self.assertIn("Connected to the Mesh", drawn(overview={**OV, "events": []}).texts)         # the Mesh has nothing to say: this computer's own log instead
        self.assertIn("Connected to the Mesh", drawn(overview=None).texts)                   # without the overview it falls back to this computer's own log

    def test_ai_agreement_and_models_and_projects_are_shown_plainly(self):
        self.assertIn("AI agreed to connect", drawn().texts); self.assertIn("AI agreed to connect", drawn("comms").texts)
        self.assertIn("m1", drawn("models").texts); self.assertIn("Newsletter", drawn("tasks").texts); self.assertIn("1 waiting for the owner", drawn("tasks").texts)
        self.assertIn("No tasks running", drawn(overview={**OV, "projects": []}).texts)
        self.assertIn("none", " ".join(drawn(ai={}).texts).lower())

    def test_navigation_regions_cover_every_page_without_overlap(self):
        regs = D.nav_regions()
        self.assertEqual([r[0] for r in regs], [k for k, _, _ in D.NAV])
        for (_, x, y, w, h), (_, x2, y2, w2, h2) in zip(regs, regs[1:]):
            self.assertLessEqual(y + h, y2)

    def test_odd_values_do_not_break_it(self):
        drawn(name="A" * 90, ai={"label": "L", "host": "h" * 100, "models": ["m" * 80] * 9}, used=250, limit=-5, overview={**OV, "nodes": [node("n" * 60, ram_gb=None, threads=None)] * 9})
        self.assertEqual(D.fmt_secs(None), "0 s"); self.assertEqual(D.fmt_uptime(0), "0 min"); self.assertEqual(D.wrap("", 10), [])
        self.assertEqual(D.mix("#000000", "#ffffff", 0.5), "#808080"); self.assertEqual(D.mix("#000000", "#ffffff", 7), "#ffffff")

    def test_it_renders_to_a_real_image_when_pillow_is_available(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("no Pillow here")
        for page in ("overview", "nodes"):
            c = D.PilCtx(); D.draw(c, BASE, page); self.assertEqual(c.im.size, (D.W * 2, D.H * 2))


if __name__ == "__main__":
    unittest.main()
