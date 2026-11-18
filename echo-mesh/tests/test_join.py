import json, os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import coordinator as CO
from mesh.coordinator import Coordinator, is_lan
from mesh.agent import Agent
from mesh.common import confirm_number


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        self.n = 0

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def agent(self):
        self.n += 1
        return Agent(f"{self.tmp.name}/a{self.n}")

    def test_is_lan(self):
        for a in ("192.168.1.20", "10.0.0.31", "172.16.4.4", "127.0.0.1", "::ffff:192.168.1.5"):
            self.assertTrue(is_lan(a), a)
        for a in ("100.66.116.80", "8.8.8.8", "2001:4860:4860::8888", "junk"):
            self.assertFalse(is_lan(a), a)

    def test_full_join_with_matching_numbers_and_one_time_secret(self):
        a = self.agent(); seen = []
        res = {}
        th = threading.Thread(target=lambda: res.update(a.join_lan("127.0.0.1", self.c.ports[0], "living-room", on_number=seen.append, poll=0.3)))
        th.start()
        end = time.time() + 10
        while not self.c.pending_requests() and time.time() < end: time.sleep(0.1)
        req = self.c.pending_requests()[0]
        self.assertEqual(req["name"], "living-room")
        self.assertEqual(seen[0], req["confirm"])            # the number on the PC equals the number shown to the owner
        self.assertEqual(seen[0], confirm_number(self.c.fp, req["id"]))
        self.assertTrue(self.c.decide_request(req["id"], True))
        th.join(10)
        self.assertEqual(res["node_type"], "compute")
        self.assertEqual(a.heartbeat_once()[0], 200)         # it is a working, authenticated node now
        poll = self.c.join_poll(req["id"], "wrong")
        self.assertIsNone(poll)                              # wrong token learns nothing

    def test_secret_is_handed_over_only_once(self):
        r, _ = self.c.join_request("x", {}, "192.168.1.9")
        rid, tok = r["request_id"], r["poll_token"]
        self.assertTrue(self.c.decide_request(rid, True))
        first = self.c.join_poll(rid, tok); second = self.c.join_poll(rid, tok)
        self.assertEqual(first["status"], "approved"); self.assertIn("secret", first)
        self.assertNotIn("secret", second)

    def test_denied_and_outside_and_limits(self):
        r, _ = self.c.join_request("x", {}, "192.168.1.9")
        self.assertTrue(self.c.decide_request(r["request_id"], False))
        self.assertEqual(self.c.join_poll(r["request_id"], r["poll_token"])["status"], "denied")
        self.assertEqual(self.c.join_request("x", {}, "8.8.8.8"), (None, "outside"))
        self.assertEqual(self.c.join_request("x", {}, "100.66.1.2"), (None, "outside"))     # tailscale is not 'home network'
        for _ in range(3): self.assertIsNotNone(self.c.join_request("y", {}, "192.168.1.50")[0])
        self.assertEqual(self.c.join_request("y", {}, "192.168.1.50"), (None, "busy"))       # 3 per address

    def test_expired_request_cannot_be_approved(self):
        r, _ = self.c.join_request("x", {}, "192.168.1.9")
        self.c.store.x("UPDATE join_requests SET created=?", (time.time() - CO.REQ_TTL_S - 5,))
        self.assertFalse(self.c.decide_request(r["request_id"], True))
        self.assertEqual(self.c.join_poll(r["request_id"], r["poll_token"])["status"], "expired")

    def test_join_can_only_make_a_compute_node(self):
        r, _ = self.c.join_request("x", {}, "192.168.1.9"); self.c.decide_request(r["request_id"], True)
        self.assertEqual(self.c.store.q("SELECT node_type, status FROM nodes")[0], {"node_type": "compute", "status": "approved"})

    def test_admin_requests_need_the_token(self):
        import urllib.request, urllib.error
        rq = urllib.request.Request(f"http://127.0.0.1:{self.c.ports[1]}/requests")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(rq, timeout=5)
        self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
