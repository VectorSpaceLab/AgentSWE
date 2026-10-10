"""`agentswe doctor` checks every address pool a task allocates Docker subnets from (stdlib unittest, no network).

    python3 -m unittest tests/test_doctor_pools.py

AGENTSWE_NETWORK_POOL is a host check; Optimization tasks also allocate from AGENTSWE_OPTIMIZATION_POOL (every task,
compose adapter), AGENTSWE_TAU3_POOL (tau3) and AGENTSWE_PINCHBENCH_POOL (PinchBench), whose defaults live in the
runner. A pool that overlaps a host route or an existing Docker network's subnet is a warning; one that is not a CIDR
block fails. Routes and Docker networks are injected here, so no command runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config, doctor  # noqa: E402
from agentswe.registry import find  # noqa: E402
from agentswe.runners.optimization_native_v1 import POOLS  # noqa: E402


def load(**env: str) -> config.Config:
    keys = [k for k in os.environ if k.startswith("AGENTSWE_")]
    saved = {k: os.environ.pop(k) for k in keys}
    try:
        os.environ.update(env)
        return config.load("/nonexistent/.env")
    finally:
        for k in env:
            os.environ.pop(k, None)
        os.environ.update(saved)


class PoolCheck(unittest.TestCase):
    def test_clear_pool_is_ok(self):
        c = doctor.pool_check("AGENTSWE_TAU3_POOL", "10.217.64.0/18", ["192.0.2.0/24", "172.17.0.0/16"], ["172.17.0.0/16"])
        self.assertEqual((c.status, c.name, c.detail), (doctor.OK, "network pool AGENTSWE_TAU3_POOL", "10.217.64.0/18"))

    def test_route_overlap_warns(self):
        c = doctor.pool_check("AGENTSWE_TAU3_POOL", "198.18.0.0/15", ["198.18.254.30", "192.0.2.0/24"], [])
        self.assertEqual(c.status, doctor.WARN)
        self.assertIn("overlaps routes ['198.18.254.30']", c.detail)

    def test_docker_network_overlap_warns_once(self):
        c = doctor.pool_check("AGENTSWE_OPTIMIZATION_POOL", "198.18.0.0/15", ["198.19.52.160/28"],
                              ["198.19.52.160/28", "198.19.53.0/28", "fd00::/64", "172.17.0.0/16"])
        self.assertEqual(c.status, doctor.WARN)
        self.assertIn("overlaps routes ['198.19.52.160/28']", c.detail)
        self.assertIn("overlaps Docker networks ['198.19.53.0/28']", c.detail)

    def test_long_lists_are_shortened(self):
        routes = [f"10.246.{i}.0/24" for i in range(8)]
        c = doctor.pool_check("AGENTSWE_NETWORK_POOL", "10.246.0.0/16", routes, [])
        self.assertEqual(c.name, "network pool")
        self.assertIn("(+3 more)", c.detail)
        self.assertNotIn("10.246.7.0/24", c.detail)

    def test_invalid_pool_fails(self):
        self.assertEqual(doctor.pool_check("AGENTSWE_TAU3_POOL", "not-a-cidr", [], []).status, doctor.FAIL)
        self.assertEqual(doctor.pool_check("AGENTSWE_TAU3_POOL", None, [], []).status, doctor.FAIL)


class TaskPools(unittest.TestCase):
    def test_pools_per_task(self):
        self.assertEqual(doctor.task_pools(find("tau3-retail")), ("AGENTSWE_OPTIMIZATION_POOL", "AGENTSWE_TAU3_POOL"))
        self.assertEqual(doctor.task_pools(find("pinchbench-openclaw")),
                         ("AGENTSWE_OPTIMIZATION_POOL", "AGENTSWE_PINCHBENCH_POOL"))
        self.assertEqual(doctor.task_pools(find("osworld")), ("AGENTSWE_OPTIMIZATION_POOL",))
        self.assertEqual(doctor.task_pools(find("repository-bug-repair")), ())
        self.assertEqual(doctor.task_pools(find("aider-worktree-transaction")), ())

    def test_every_pool_has_a_runner_default(self):
        for name in {p for pools in doctor.OPTIMIZATION_TASK_POOLS.values() for p in pools} | {"AGENTSWE_OPTIMIZATION_POOL"}:
            self.assertIn(name, POOLS)

    def test_task_checks_use_defaults_and_settings(self):
        networks = (["198.18.254.30", "192.0.2.0/24"], ["198.19.52.160/28"])
        with tempfile.TemporaryDirectory() as home, mock.patch.object(doctor, "host_networks", return_value=networks):
            checks = {c.name: c for c in doctor.task_checks(load(AGENTSWE_HOME=home), find("tau3-retail"))}
            self.assertEqual(checks["network pool AGENTSWE_TAU3_POOL"].status, doctor.WARN)
            self.assertTrue(checks["network pool AGENTSWE_TAU3_POOL"].detail.startswith(POOLS["AGENTSWE_TAU3_POOL"]))
            self.assertEqual(checks["network pool AGENTSWE_OPTIMIZATION_POOL"].status, doctor.WARN)
            self.assertNotIn("network pool AGENTSWE_PINCHBENCH_POOL", checks)

            cfg = load(AGENTSWE_HOME=home, AGENTSWE_TAU3_POOL="10.217.64.0/18",
                       AGENTSWE_OPTIMIZATION_POOL="10.217.128.0/18")
            checks = {c.name: c for c in doctor.task_checks(cfg, find("tau3-retail"))}
            self.assertEqual(checks["network pool AGENTSWE_TAU3_POOL"].status, doctor.OK)
            self.assertEqual(checks["network pool AGENTSWE_OPTIMIZATION_POOL"].status, doctor.OK)

            checks = {c.name for c in doctor.task_checks(load(AGENTSWE_HOME=home), find("repository-bug-repair"))}
            self.assertFalse(any(name.startswith("network pool") for name in checks))



class OwnRunNetworks(unittest.TestCase):
    """While this home's runs are active, their /28 networks (each also a route on its bridge) are not overlaps."""

    def home(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "network_allocations").mkdir()
        (home / "network_allocations" / "allocations.json").write_text(
            '{"a1": {"subnet": "10.229.0.0/28", "compose": "/x/docker-compose.yaml"}}')
        (home / "coordination" / "compose").mkdir(parents=True)
        (home / "coordination" / "compose" / "network_ipam_registry.json").write_text(
            '{"assignments": {"k": "10.229.64.16/28"}, "base_cidr": "10.229.64.0/18", "schema_version": "1.0",'
            ' "subnet_prefix": 28}')
        (home / "coordination" / "pinchbench").mkdir()
        (home / "coordination" / "pinchbench" / "pinchbench_network_ipam_registry.json").write_text(
            '{"allocations": {"p:g": ["10.229.128.0/28", "10.229.128.16/28"]}}')
        return home

    def test_home_subnets_reads_every_registry_but_not_the_base(self):
        self.assertEqual(doctor.home_subnets(self.home()),
                         {"10.229.0.0/28", "10.229.64.16/28", "10.229.128.0/28", "10.229.128.16/28"})

    def test_own_networks_are_not_overlaps(self):
        own = doctor.home_subnets(self.home())
        check = doctor.pool_check("AGENTSWE_NETWORK_POOL", "10.229.0.0/18", ["10.229.0.0/28"], ["10.229.0.0/28"], own)
        self.assertEqual(check.status, doctor.OK)
        self.assertIn("1 subnet(s) in use by this home's runs", check.detail)

    def test_foreign_networks_still_warn(self):
        own = doctor.home_subnets(self.home())
        check = doctor.pool_check("AGENTSWE_NETWORK_POOL", "10.229.0.0/18",
                                  ["10.229.0.0/28", "10.229.1.0/28"], ["10.229.0.0/28", "10.229.2.0/28"], own)
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("10.229.1.0/28", check.detail)
        self.assertIn("10.229.2.0/28", check.detail)
        self.assertNotIn("'10.229.0.0/28'", check.detail)

    def test_pool_equal_to_a_registry_base_still_warns(self):
        own = doctor.home_subnets(self.home())
        check = doctor.pool_check("AGENTSWE_OPTIMIZATION_POOL", "10.229.64.0/18", [], ["10.229.64.0/18"], own)
        self.assertEqual(check.status, doctor.WARN)

    def test_missing_registries_change_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(doctor.home_subnets(Path(tmp.name)), set())

if __name__ == "__main__":
    unittest.main()
