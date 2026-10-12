"""A runner's per-run client and stats tokens reach the broker in its environment (stdlib unittest, fake provider).

The Creation and Optimization runners start each provider broker with AGENTSWE_BROKER_CLIENT_TOKEN and
AGENTSWE_BROKER_STATS_TOKEN set (passed by name, so they are on no command line). The broker then accepts only that
client token and that stats token: none of the public placeholders. Without them it keeps the legacy placeholders
(standalone use).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FAKE_KEY, FakeProvider, Script, responses_completed, tmpdir  # noqa: E402

PUBLIC = ("broker-only-placeholder", "runtime-only-placeholder", "judge-only-placeholder")


def call(url: str, token: str, *, body: bytes | None = b'{"input":"q"}') -> int:
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class ClientTokenFromEnvironment(unittest.TestCase):
    def serve(self, role: str, extra_env: dict[str, str]):
        self.provider = FakeProvider()
        self.work = tmpdir()
        envfile = self.work / ".env"
        envfile.write_text("AGENTSWE_DEFAULT_BASE_URL=%s\nAGENTSWE_DEFAULT_API_KEY=%s\n" % (self.provider.base, FAKE_KEY))
        os.chmod(envfile, 0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}
        env.update(PYTHONPATH=str(ROOT), **extra_env)
        self.process = subprocess.Popen([sys.executable, "-m", "agentswe_broker", "serve", "--role", role,
                                         "--env-file", str(envfile), "--stats-file", str(self.work / "s.json"),
                                         "--ledger-dir", str(self.work / "l"), "--port", "0"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.ready = json.loads(self.process.stdout.readline())
        return "http://127.0.0.1:%d" % self.ready["port"]

    def tearDown(self):
        self.process.terminate()
        self.output = self.process.communicate(timeout=10)
        self.provider.close()

    def test_runner_tokens_replace_every_public_placeholder(self):
        client, stats = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        base = self.serve("judge", {"AGENTSWE_BROKER_CLIENT_TOKEN": client, "AGENTSWE_BROKER_STATS_TOKEN": stats})
        for token in PUBLIC + (stats, ""):
            self.assertEqual(call(base + "/v1/responses", token), 401, token)
        self.provider.add(Script(200, json.dumps(responses_completed("x")).encode()))
        self.assertEqual(call(base + "/v1/responses", client), 200)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(call(base + "/stats", "stats-only-placeholder", body=None), 401)
        self.assertEqual(call(base + "/stats", client, body=None), 401)
        self.assertEqual(call(base + "/stats", stats, body=None), 200)
        self.assertNotIn(client, json.dumps(self.ready))

    def test_without_runner_tokens_the_legacy_placeholders_stay(self):
        base = self.serve("runtime", {})
        self.provider.add(Script(200, json.dumps(responses_completed("x")).encode()))
        self.assertEqual(call(base + "/v1/responses", "runtime-only-placeholder"), 200)
        self.assertEqual(call(base + "/stats", "stats-only-placeholder", body=None), 200)


if __name__ == "__main__":
    unittest.main()
