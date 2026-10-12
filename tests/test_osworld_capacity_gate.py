"""OSWorld VM capacity gate counts reclaimable memory per NUMA node (stdlib unittest, synthetic sysfs, no VM).

    python3 -m unittest tests/test_osworld_capacity_gate.py

A node qualifies when MemFree + Inactive(file) + KReclaimable reaches the threshold: 8 GiB, as in the paper gate,
unless AGENTSWE_OSWORLD_MIN_NUMA_FREE_GIB overrides it. The paper gate counted MemFree alone.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "runners" / "optimization" / "optimization_native"
GIB = 1024 * 1024  # KiB


def meminfo(node: int, memfree: int, inactive_file: int, kreclaimable: int | None) -> str:
    lines = [f"Node {node} MemTotal:       {128 * GIB} kB", f"Node {node} MemFree:        {memfree} kB",
             f"Node {node} Active(file):   {5 * GIB} kB", f"Node {node} Inactive(file): {inactive_file} kB",
             f"Node {node} Shmem:          {30 * GIB} kB"]
    if kreclaimable is not None:
        lines.append(f"Node {node} KReclaimable:   {kreclaimable} kB")
    return "\n".join(lines) + "\n"


class CapacityGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stub = types.ModuleType("osworld_provider")  # needs requests and Pillow; only constants are used here
        stub.AUDIT_LABEL, stub.PROVIDER_DIGEST, stub.PROVIDER_IMAGE = "label", "sha256:0", "image"
        stub.PROVIDER_OVERLAY_ROOT, stub.PROVIDER_PULL_IMAGE = Path(tempfile.gettempdir()), "image"
        saved = sys.modules.get("osworld_provider")
        sys.modules["osworld_provider"] = stub
        sys.path.insert(0, str(NATIVE))
        try:
            cls.c = importlib.import_module("osworld_controller")
        finally:
            sys.path.remove(str(NATIVE))
            if saved is None:
                sys.modules.pop("osworld_provider", None)
            else:
                sys.modules["osworld_provider"] = saved

    def evidence(self, nodes: dict[int, tuple[int, int, int | None]], threshold_gib: str | None = None) -> dict:
        saved = os.environ.pop(self.c.NUMA_THRESHOLD_ENV, None)
        try:
            if threshold_gib is not None:
                os.environ[self.c.NUMA_THRESHOLD_ENV] = threshold_gib
            with tempfile.TemporaryDirectory() as tmp:
                for node, values in nodes.items():
                    (Path(tmp) / f"node{node}").mkdir()
                    (Path(tmp) / f"node{node}" / "meminfo").write_text(meminfo(node, *values))
                host = {"numa_nodes_online": [str(n) for n in nodes]}
                return self.c.numa_capacity_evidence(host, sysfs=Path(tmp))
        finally:
            os.environ.pop(self.c.NUMA_THRESHOLD_ENV, None)
            if saved is not None:
                os.environ[self.c.NUMA_THRESHOLD_ENV] = saved

    def test_page_cache_counts(self):
        """A host with MemFree under 7 GiB on every node and reclaimable page cache."""
        ev = self.evidence({0: (3 * GIB, 40 * GIB, 2 * GIB), 1: (6 * GIB, 1 * GIB, 512)})
        self.assertTrue(ev["ready"])
        self.assertEqual(ev["eligible_numa_nodes"], ["0"])
        self.assertEqual(ev["numa_reclaimable_kib"]["0"], 45 * GIB)
        self.assertEqual(ev["numa_memfree_kib"], {"0": 3 * GIB, "1": 6 * GIB})
        self.assertEqual((ev["min_numa_free_kib"], ev["min_numa_free_source"]), (8 * GIB, "default"))

    def test_memfree_alone_is_the_paper_floor(self):
        ev = self.evidence({0: (9 * GIB, 0, None)})
        self.assertEqual(ev["eligible_numa_nodes"], ["0"])  # a node the paper gate admitted is still admitted

    def test_short_node_waits(self):
        ev = self.evidence({0: (2 * GIB, 3 * GIB, 1 * GIB), 1: (1 * GIB, 6 * GIB, None)})
        self.assertFalse(ev["ready"])
        self.assertEqual(ev["eligible_numa_nodes"], [])

    def test_shmem_and_active_file_do_not_count(self):
        ev = self.evidence({0: (1 * GIB, 1 * GIB, 1 * GIB)})  # meminfo also lists 30 GiB Shmem and 5 GiB Active(file)
        self.assertEqual(ev["numa_reclaimable_kib"]["0"], 3 * GIB)
        self.assertFalse(ev["ready"])

    def test_env_override(self):
        ev = self.evidence({0: (2 * GIB, 3 * GIB, 0)}, threshold_gib="4")
        self.assertEqual((ev["ready"], ev["min_numa_free_kib"], ev["min_numa_free_source"]),
                         (True, 4 * GIB, "AGENTSWE_OSWORLD_MIN_NUMA_FREE_GIB"))
        for bad in ("zero", "0", "-1"):
            with self.assertRaises(self.c.CapacityGateError):
                self.evidence({0: (9 * GIB, 0, 0)}, threshold_gib=bad)

    def test_setting_reaches_the_manifest(self):
        task = json.loads((ROOT / "tasks" / "optimization" / "osworld" / "task.json").read_text())
        self.assertIn("AGENTSWE_OSWORLD_MIN_NUMA_FREE_GIB", task["runner_config"]["config_env"])


if __name__ == "__main__":
    unittest.main()
