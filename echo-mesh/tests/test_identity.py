import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator
from mesh import inventory

MID_A, MID_B = "a" * 24, "b" * 24


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def join(self, name, mid, approve=True):
        r, _ = self.c.join_request(name, {"machine_id": mid, "cpu_threads": 4}, "192.168.1.50")
        req = self.c.pending_requests()[-1]
        self.assertTrue(self.c.decide_request(req["id"], approve))
        return self.c.join_poll(r["request_id"], r["poll_token"])

    def status(self, nid):
        return self.c.store.one("SELECT status FROM nodes WHERE id=?", (nid,))["status"]

    def test_machine_id_is_stable_and_hashed(self):
        a = inventory.machine_id()
        self.assertEqual(a, inventory.machine_id())
        if a:
            self.assertEqual(len(a), 24)

    def test_rejoin_same_computer_retires_the_old_entry(self):
        first = self.join("pc", MID_A)
        r, _ = self.c.join_request("pc", {"machine_id": MID_A}, "192.168.1.50")
        self.assertEqual(self.c.pending_requests()[-1]["duplicate_of"], ["pc"])      # the owner is told before approving
        self.c.decide_request(self.c.pending_requests()[-1]["id"], True)
        second = self.c.join_poll(r["request_id"], r["poll_token"])
        self.assertEqual(self.status(first["node_id"]), "revoked")
        self.assertEqual(self.status(second["node_id"]), "approved")

    def test_different_computers_do_not_collide(self):
        a = self.join("pc1", MID_A); b = self.join("pc2", MID_B)
        self.assertEqual(self.status(a["node_id"]), "approved")
        self.assertEqual(self.status(b["node_id"]), "approved")

    def test_copied_install_is_paused(self):
        a = self.join("pc", MID_A)
        node = self.c.store.one("SELECT * FROM nodes WHERE id=?", (a["node_id"],))
        self.assertTrue(self.c.heartbeat(node, {"machine_id": MID_A})["ok"])
        self.assertEqual(self.c.heartbeat(node, {"machine_id": MID_B}), {"ok": False, "error": "identity-conflict"})
        self.assertEqual(self.status(a["node_id"]), "paused")

    def test_old_agent_without_id_is_remembered_once(self):
        a = self.join("pc", None)
        node = self.c.store.one("SELECT * FROM nodes WHERE id=?", (a["node_id"],))
        self.c.heartbeat(node, {"machine_id": MID_A})
        node = self.c.store.one("SELECT * FROM nodes WHERE id=?", (a["node_id"],))
        self.assertEqual(json.loads(node["inventory"])["machine_id"], MID_A)
        self.assertFalse(self.c.heartbeat(node, {"machine_id": MID_B})["ok"])


if __name__ == "__main__":
    unittest.main()
