"""AGENTSWE_OSWORLD_EFFORT reaches the OSWorld vision broker's upstream request (stdlib unittest, no network).

    python3 -m unittest tests/test_osworld_effort.py

The chain: the runner exports the task's config_env settings (task_config_env) to the controller, the controller
passes AGENTSWE_OSWORLD_EFFORT into the broker container (broker_settings_args) next to the mounted
agentswe_broker package, and the broker reads it with normalize_effort's spellings: none / off / unset leave the
effort out of the request, explicit-none sends the literal "none", any other value is sent as given; unset keeps
the paper's "high"; the smoke defaults set explicit-none (agentswe/config.py smoke_osworld_effort). The broker runs
here in the container's file layout (/broker.py, /osworld_agent.py, /agentswe_broker) against a local fake upstream.
"""
from __future__ import annotations

import base64
import http.server
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "runners" / "optimization" / "optimization_native"
TASK = ROOT / "tasks" / "optimization" / "osworld" / "task.json"
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners.optimization_native_v1 import task_config_env  # noqa: E402

TOKEN = "r" * 40
STATS = "s" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ABSENT = object()


class FakeUpstream:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
                text = json.dumps({"action": {"kind": "done", "x": None, "y": None, "button": None, "text": None,
                                              "key": None, "keys": None, "amount": None, "seconds": None,
                                              "rationale": "ok"}})
                data = json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                                   "usage": {"total_tokens": 7}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/responses"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def broker_request(upstream: str, *, env_setting: str | None = None, file_setting: str | None = None) -> dict:
    """One action request through osworld_broker.py laid out as in its container; returns the broker stats."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copy(NATIVE / "osworld_broker.py", root / "broker.py")
        shutil.copy(NATIVE / "osworld_agent.py", root / "osworld_agent.py")
        shutil.copytree(ROOT / "broker" / "agentswe_broker", root / "agentswe_broker")
        cred = root / "agentswe.env"
        lines = {"AGENTSWE_RUNTIME_API_KEY": "k", "AGENTSWE_RUNTIME_RESPONSES_URL": upstream,
                 "AGENTSWE_RUNTIME_MODEL": "m"}
        if file_setting is not None:
            lines["AGENTSWE_OSWORLD_EFFORT"] = file_setting
        cred.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}
        env.update({"PYTHONPATH": str(root), "AGENTSWE_OSWORLD_SCHEMA_STRICT": "0"})
        if env_setting is not None:
            env["AGENTSWE_OSWORLD_EFFORT"] = env_setting
        port = free_port()
        proc = subprocess.Popen([sys.executable, str(root / "broker.py"), "--credential-file", str(cred),
                                 "--bind", "127.0.0.1", "--port", str(port), f"--runtime-token={TOKEN}",
                                 f"--stats-token={STATS}"], cwd=str(root), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(200):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    if proc.poll() is not None:
                        raise RuntimeError("broker exited: " + proc.stderr.read().decode()[-800:])
                    time.sleep(0.05)
            image = "data:image/png;base64," + base64.b64encode(PNG).decode()
            body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "t"},
                                                           {"type": "input_image", "image_url": image}]}],
                    "screen": {"width": 1920, "height": 1080}}
            request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/osworld/action", method="POST",
                                             data=json.dumps(body).encode(),
                                             headers={"Authorization": f"Bearer {TOKEN}",
                                                      "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                assert response.status == 200
            stats = urllib.request.Request(f"http://127.0.0.1:{port}/stats", headers={"Authorization": f"Bearer {STATS}"})
            with urllib.request.urlopen(stats, timeout=20) as response:
                return json.loads(response.read())
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stderr.close()


class BrokerRequestBody(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream()

    def tearDown(self):
        self.up.close()

    def sent(self, **kwargs) -> tuple[object, dict]:
        stats = broker_request(self.up.url, **kwargs)
        body = self.up.bodies[-1]
        self.assertEqual((body["model"], body["text"]), ("m", {"format": {"type": "json_object"}}))
        return body.get("reasoning", ABSENT), stats

    def test_each_setting(self):
        cases = [(None, {"effort": "high"}, "high"),           # unset: the paper's effort
                 ("explicit-none", {"effort": "none"}, "none"),  # DeepSeek's no-thinking mode
                 ("EXPLICIT-NONE", {"effort": "none"}, "none"),
                 ("none", ABSENT, None), ("off", ABSENT, None), ("unset", ABSENT, None),  # left out of the request
                 ("low", {"effort": "low"}, "low"), ("high", {"effort": "high"}, "high")]
        for setting, reasoning, effective in cases:
            with self.subTest(setting=setting):
                sent, stats = self.sent(env_setting=setting)
                self.assertEqual(sent, reasoning)
                self.assertEqual(stats["reasoning_effort"], effective)
                self.assertEqual((stats["calls"], stats["schema_strict"]), (1, False))

    def test_credential_file_setting_wins(self):
        sent, stats = self.sent(env_setting="low", file_setting="explicit-none")
        self.assertEqual((sent, stats["reasoning_effort"]), ({"effort": "none"}, "none"))


class ControllerPassesSetting(unittest.TestCase):
    """The controller forwards the setting into the broker container and mounts the shared effort module."""

    @classmethod
    def setUpClass(cls):
        # osworld_provider needs requests and Pillow (the OSWorld venv); only its constants are used here.
        stub = types.ModuleType("osworld_provider")
        stub.AUDIT_LABEL, stub.PROVIDER_DIGEST, stub.PROVIDER_IMAGE = "label", "sha256:0", "image"
        stub.PROVIDER_OVERLAY_ROOT, stub.PROVIDER_PULL_IMAGE = Path(tempfile.gettempdir()), "image"
        saved = sys.modules.get("osworld_provider")
        sys.modules["osworld_provider"] = stub
        sys.path.insert(0, str(NATIVE))
        try:
            cls.controller = importlib.import_module("osworld_controller")
        finally:
            sys.path.remove(str(NATIVE))
            if saved is None:
                sys.modules.pop("osworld_provider", None)
            else:
                sys.modules["osworld_provider"] = saved

    def args_with(self, value: str | None) -> list[str]:
        saved = os.environ.pop("AGENTSWE_OSWORLD_EFFORT", None)
        try:
            if value is not None:
                os.environ["AGENTSWE_OSWORLD_EFFORT"] = value
            return self.controller.broker_settings_args()
        finally:
            os.environ.pop("AGENTSWE_OSWORLD_EFFORT", None)
            if saved is not None:
                os.environ["AGENTSWE_OSWORLD_EFFORT"] = saved

    def test_effort_passed_only_when_set(self):
        self.assertIn("AGENTSWE_OSWORLD_EFFORT=explicit-none", self.args_with("explicit-none"))
        self.assertIn("AGENTSWE_OSWORLD_EFFORT=low", self.args_with("low"))
        for unset in (None, ""):
            args = self.args_with(unset)
            self.assertFalse(any(a.startswith("AGENTSWE_OSWORLD_EFFORT") for a in args))
            self.assertTrue(any(a.startswith("AGENTSWE_OSWORLD_SCHEMA_STRICT=") for a in args))

    def test_broker_package_mounted(self):
        self.assertEqual(self.controller.BROKER_PACKAGE.resolve(), (ROOT / "broker" / "agentswe_broker").resolve())
        self.assertTrue((self.controller.BROKER_PACKAGE / "config.py").is_file())
        source = (NATIVE / "osworld_controller.py").read_text()
        self.assertIn('"-v", f"{BROKER_PACKAGE}:/agentswe_broker:ro"', source)
        self.assertIn("*broker_settings_args(),", source)
        self.assertIn('"-e", "PYTHONPATH=/"', source)


class RunnerExportsSetting(unittest.TestCase):
    """task.json declares the setting in config_env; the runner exports it as resolved from .env."""

    def config_env(self, lines: str) -> dict[str, str]:
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENTSWE_")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(lines)
                rc = json.loads(TASK.read_text())["runner_config"]
                return task_config_env(config.load(env_file), rc)
        finally:
            os.environ.update(saved)

    def test_declared(self):
        self.assertIn("AGENTSWE_OSWORLD_EFFORT", json.loads(TASK.read_text())["runner_config"]["config_env"])

    def test_exported_from_dotenv(self):
        self.assertEqual(self.config_env("AGENTSWE_OSWORLD_EFFORT=explicit-none\n")["AGENTSWE_OSWORLD_EFFORT"],
                         "explicit-none")
        self.assertEqual(self.config_env("AGENTSWE_PROFILE=paper\nAGENTSWE_OSWORLD_EFFORT=low\n")
                         ["AGENTSWE_OSWORLD_EFFORT"], "low")

    def test_smoke_default_reaches_the_broker_as_none(self):
        """No profile and nothing configured: the smoke default explicit-none, sent upstream as "none"
        (BrokerRequestBody.test_each_setting)."""
        exported = self.config_env("AGENTSWE_DEFAULT_API_KEY=k\n")
        self.assertEqual((exported["AGENTSWE_OSWORLD_EFFORT"], exported["AGENTSWE_OSWORLD_SCHEMA_STRICT"]),
                         ("explicit-none", "0"))

    def test_paper_profile_leaves_it_unset(self):
        exported = self.config_env("AGENTSWE_PROFILE=paper\n")
        self.assertNotIn("AGENTSWE_OSWORLD_EFFORT", exported)
        self.assertEqual(exported["AGENTSWE_OSWORLD_SCHEMA_STRICT"], "1")


if __name__ == "__main__":
    unittest.main()
