"""The OSWorld provider container is pinned to CPUs only when asked (stdlib unittest; Docker, requests, Pillow and the
OSWorld package are faked).

The provider ran with --cpuset-cpus 0-15,128-143 --cpuset-mems 0 by default, the paper hosts' NUMA node 0. Docker
refuses that container on a host with fewer than 144 CPUs, so every attempt failed as infrastructure and a held-out
phase resumed without end. The pinning was performance isolation only: the container is now not pinned unless
OSWORLD_PROVIDER_CPUSET / OSWORLD_PROVIDER_CPUSET_MEMS are set, and both are task config_env settings so .env works."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "runners" / "optimization" / "optimization_native"
TASK = ROOT / "tasks" / "optimization" / "osworld" / "task.json"
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners.optimization_native_v1 import task_config_env  # noqa: E402

DIGEST = "sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9"


class FakeContainers:
    def __init__(self):
        self.runs = []

    def run(self, image, **kwargs):
        self.runs.append(kwargs)
        return types.SimpleNamespace(remove=lambda **_: None)


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.images = types.SimpleNamespace(get=lambda ref: types.SimpleNamespace(attrs={"RepoDigests": [f"x@{DIGEST}"]}))


def fake_modules() -> dict[str, types.ModuleType]:
    mods = {name: types.ModuleType(name) for name in (
        "requests", "PIL", "PIL.Image", "filelock", "desktop_env", "desktop_env.providers",
        "desktop_env.providers.docker", "desktop_env.providers.docker.manager",
        "desktop_env.providers.docker.provider", "desktop_env.desktop_env")}
    mods["requests"].RequestException = Exception
    mods["PIL"].Image = mods["PIL.Image"]

    class FileLock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class DockerProvider:
        def __init__(self, region):
            self.lock_file, self.container, self.client = "lock", None, FakeClient()

        def _get_used_ports(self):
            return set()

    mods["filelock"].FileLock = FileLock
    mods["desktop_env.providers.docker.manager"].DockerVMManager = type("DockerVMManager", (), {})
    mods["desktop_env.providers.docker.provider"].DockerProvider = DockerProvider
    return mods


def provider_run_kwargs(env: dict[str, str]) -> dict:
    """The keyword arguments the provider passes to docker containers.run, with `env` set."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OSWORLD_PROVIDER_CPUSET")}
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
            mock.patch.dict(sys.modules, fake_modules()):
        spec = importlib.util.spec_from_file_location("osworld_provider_cpuset_test", NATIVE / "osworld_provider.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.PROVIDER_OVERLAY_ROOT = Path(tmp) / "overlays"
        boot = []
        for name in ("vmlinuz", "initrd", "entry.sh", "vm.qcow2"):
            (Path(tmp) / name).write_bytes(b"x")
            boot.append(str(Path(tmp) / name))
        cls = module.install_provider_override("run-1", *boot[:3])
        cls._wait_for_stable_desktop = lambda self, timeout=0: None
        provider = cls("local")
        real_exists = os.path.exists
        with mock.patch("os.path.exists", side_effect=lambda p: p == "/dev/kvm" or real_exists(p)):
            provider.start_emulator(boot[3], headless=True, os_type="Ubuntu")
        (kwargs,) = provider.client.containers.runs
        return kwargs


class ProviderCpuset(unittest.TestCase):
    def test_not_pinned_by_default(self):
        kwargs = provider_run_kwargs({})
        self.assertNotIn("cpuset_cpus", kwargs)
        self.assertNotIn("cpuset_mems", kwargs)
        self.assertEqual(kwargs["cpu_shares"], 8192)

    def test_pinned_when_set(self):
        kwargs = provider_run_kwargs({"OSWORLD_PROVIDER_CPUSET": "0-15,128-143", "OSWORLD_PROVIDER_CPUSET_MEMS": "0"})
        self.assertEqual((kwargs["cpuset_cpus"], kwargs["cpuset_mems"]), ("0-15,128-143", "0"))
        kwargs = provider_run_kwargs({"OSWORLD_PROVIDER_CPUSET": "2-5"})
        self.assertEqual(kwargs["cpuset_cpus"], "2-5")
        self.assertNotIn("cpuset_mems", kwargs)

    def test_settings_come_from_dotenv(self):
        rc = json.loads(TASK.read_text())["runner_config"]
        self.assertIn("OSWORLD_PROVIDER_CPUSET", rc["config_env"])
        self.assertIn("OSWORLD_PROVIDER_CPUSET_MEMS", rc["config_env"])
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("AGENTSWE_", "OSWORLD_"))}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text("OSWORLD_PROVIDER_CPUSET=0-7\nOSWORLD_PROVIDER_CPUSET_MEMS=0\n")
                exported = task_config_env(config.load(env_file), rc)
                self.assertEqual((exported["OSWORLD_PROVIDER_CPUSET"], exported["OSWORLD_PROVIDER_CPUSET_MEMS"]),
                                 ("0-7", "0"))
                env_file.write_text("\n")
                self.assertNotIn("OSWORLD_PROVIDER_CPUSET", task_config_env(config.load(env_file), rc))
        finally:
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
