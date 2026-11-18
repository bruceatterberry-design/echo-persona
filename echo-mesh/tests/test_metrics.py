import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import inventory

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                          100000.
Pages wired down:                         50000.
Pages occupied by compressor:             30000.
"""
MI = "MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    6000000 kB\n"


class Metrics(unittest.TestCase):
    def test_vm_stat_counts_active_wired_compressed(self):
        used, total = inventory.parse_vm_stat(VM, 17179869184)
        self.assertEqual(used, (200000 + 50000 + 30000) * 16384); self.assertEqual(total, 17179869184)

    def test_meminfo(self):
        self.assertEqual(inventory.parse_meminfo(MI), (10000000 * 1024, 16000000 * 1024))

    def test_load_reports_only_what_it_measured(self):
        d = inventory.load()
        for k in d:
            self.assertIsNotNone(d[k])                                   # never a guessed or empty value
        self.assertTrue(set(d) <= {"load1", "cpu_pct", "mem_used_bytes", "mem_total_bytes", "gpu_pct", "gpu_mem_mb"})


if __name__ == "__main__":
    unittest.main()
