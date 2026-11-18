import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator
from mesh.agent import Agent, discover, discover_info, hint_hosts, _directed_broadcasts
from mesh.common import local_ipv4s


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        import time
        end = time.time() + 5
        while not self.c.ports[2] and time.time() < end:      # the beacon thread binds a moment after start()
            time.sleep(0.05)

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def test_beacon_lists_the_addresses_and_unicast_reaches_it(self):
        i = discover_info(target="127.0.0.1", port=self.c.ports[2])
        self.assertEqual(i["port"], self.c.ports[0]); self.assertIsInstance(i["addrs"], list)
        self.assertEqual(discover(target="127.0.0.1", port=self.c.ports[2])[1], self.c.ports[0])     # old triple still works

    def test_known_hosts_file_and_env_are_read(self):
        open(self.tmp.name + "/known-hosts.txt", "w").write("# the Mac on the other network\n192.168.1.20\n\n10.0.0.5\n")
        os.environ["ECHOMESH_HINTS"] = "10.0.0.5, 100.66.1.1"
        try:
            self.assertEqual(hint_hosts(self.tmp.name), ["10.0.0.5", "100.66.1.1", "192.168.1.20"])
        finally:
            del os.environ["ECHOMESH_HINTS"]

    def test_directed_broadcasts_match_local_networks(self):
        for b in _directed_broadcasts():
            self.assertTrue(b.endswith(".255"))
        self.assertTrue(all(ip.count(".") == 3 for ip in local_ipv4s()))

    def test_agent_finds_the_mesh_again_at_a_new_address_but_not_an_impostor(self):
        a = Agent(self.tmp.name + "/a"); a.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute", "owner", True), "pc")
        a.ident["host"] = "203.0.113.9"; a._save()                       # the Mesh "moved"
        open(a.dir + "/known-hosts.txt", "w").write("127.0.0.1\n")      # the one known way across
        self.assertTrue(a.rediscover(port=self.c.ports[2], timeout=2))
        self.assertEqual(a.ident["host"], "127.0.0.1")
        a.ident["pin"] = "0" * 64; a.ident["host"] = "203.0.113.9"      # a different certificate answers: refused
        self.assertFalse(a.rediscover(port=self.c.ports[2], timeout=2))
        self.assertEqual(a.ident["host"], "203.0.113.9")


if __name__ == "__main__":
    unittest.main()
