"""Search shim, role configuration and the CLI (no secrets on stdout/stderr)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FAKE_KEY, FakeProvider, RunningBroker, Script, assert_no_key, tmpdir  # noqa: E402
from agentswe_broker import config  # noqa: E402
from agentswe_broker.server import Options  # noqa: E402


class SearchShim(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.work = tmpdir()
        self.brokers = []

    def tearDown(self):
        for item in self.brokers:
            item.close()
        self.provider.close()

    def broker(self, wire):
        url = self.provider.base.replace("/v1", "") + ("/serp" if wire == "legacy-proxy" else "")
        running = RunningBroker(Options(role="search", upstream_wire=wire, provider_url=url,
                                        stats_file=self.work / "search.json",
                                        ledger_dir=self.work / ("ledger-" + wire), keepalive_seconds=0.2))
        self.brokers.append(running)
        return running

    def test_legacy_request_becomes_serper_request(self):
        reply = {"organic": [{"title": "t", "link": "https://example.org", "snippet": "s"}]}
        self.provider.add(Script(200, json.dumps(reply).encode()))
        broker = self.broker("serper")
        status, _, payload = broker.post("/serp_search_v1", {
            "query": "agent benchmarks", "page": 2, "search_type": "news", "token": "broker-only-placeholder"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), reply)
        sent = self.provider.requests[0]
        self.assertEqual(sent["path"], "/news")
        self.assertEqual(sent["headers"]["X-Api-Key"], FAKE_KEY)
        self.assertEqual(sent["body"], {"q": "agent benchmarks", "page": 2})
        self.assertNotIn("Authorization", sent["headers"])
        assert_no_key(self, self.work)

    def test_legacy_proxy_upstream_keeps_the_legacy_shape(self):
        self.provider.add(Script(200, b'{"organic": []}'))
        broker = self.broker("legacy-proxy")
        broker.post("/serp_search_v1", {"query": "q", "token": "broker-only-placeholder"})
        sent = self.provider.requests[0]
        self.assertEqual(sent["path"], "/serp")
        self.assertEqual(sent["body"], {"query": "q", "page": 1, "search_type": "search", "token": FAKE_KEY})

    def test_serper_native_client(self):
        self.provider.add(Script(200, b'{"organic": []}'))
        broker = self.broker("serper")
        status, _, _ = broker.post("/search", {"q": "x", "page": 1}, token="unused",
                                   headers={"X-API-KEY": "broker-only-placeholder"})
        self.assertEqual(status, 200)
        self.assertEqual(self.provider.requests[0]["body"], {"q": "x", "page": 1})

    def test_real_or_missing_token_is_refused(self):
        broker = self.broker("serper")
        for token in (FAKE_KEY, None):
            body = {"query": "q"}
            if token:
                body["token"] = token
            status, _, _ = broker.post("/serp_search_v1", body)
            self.assertEqual(status, 401)
        self.assertEqual(self.provider.requests, [])

    def test_invalid_search_type_is_a_request_error(self):
        broker = self.broker("serper")
        status, _, _ = broker.post("/serp_search_v1", {"query": "q", "search_type": "scrape",
                                                       "token": "broker-only-placeholder"})
        self.assertEqual(status, 400)
        self.assertEqual(broker.stats()["runtime"]["classifications"], {"request_error": 1})


class RoleConfig(unittest.TestCase):
    def test_default_fallback_and_lite_defaults(self):
        env = {"AGENTSWE_DEFAULT_BASE_URL": "https://api.deepseek.com/v1", "AGENTSWE_DEFAULT_API_KEY": "k1"}
        for role, effort in (("builder", "max"), ("runtime", "high"), ("judge", "max")):
            cfg = config.resolve(role, env)
            self.assertEqual((cfg.base_url, cfg.wire, cfg.model, cfg.effort, cfg.api_key),
                             ("https://api.deepseek.com/v1", "responses", "deepseek-flash", effort, "k1"))
            self.assertEqual(cfg.endpoint(), "https://api.deepseek.com/v1/responses")

    def test_role_overrides_default(self):
        env = {"AGENTSWE_DEFAULT_BASE_URL": "https://a/v1", "AGENTSWE_DEFAULT_API_KEY": "k1",
               "AGENTSWE_BUILDER_BASE_URL": "https://api.deepinfra.com/v1/openai",
               "AGENTSWE_BUILDER_API_KEY": "k2", "AGENTSWE_BUILDER_WIRE": "chat",
               "AGENTSWE_BUILDER_MODEL": "Qwen/Qwen3.6-35B-A3B", "AGENTSWE_BUILDER_EFFORT": "none"}
        cfg = config.resolve("builder", env)
        self.assertEqual(cfg.endpoint(), "https://api.deepinfra.com/v1/openai/chat/completions")
        self.assertEqual((cfg.api_key, cfg.api_key_var, cfg.effort), ("k2", "AGENTSWE_BUILDER_API_KEY", None))
        self.assertEqual(config.resolve("judge", env).base_url, "https://a/v1")

    def test_search_never_falls_back_to_the_model_default(self):
        env = {"AGENTSWE_DEFAULT_BASE_URL": "https://a/v1", "AGENTSWE_DEFAULT_API_KEY": "k1"}
        with self.assertRaises(config.ConfigError):
            config.resolve("search", env)
        env.update({"AGENTSWE_SEARCH_BASE_URL": "https://google.serper.dev", "AGENTSWE_SEARCH_API_KEY": "s"})
        cfg = config.resolve("search", env)
        self.assertEqual((cfg.wire, cfg.endpoint()), ("serper", "https://google.serper.dev"))

    def test_errors(self):
        with self.assertRaises(config.ConfigError):
            config.resolve("judge", {"AGENTSWE_DEFAULT_BASE_URL": "https://a/v1"})
        with self.assertRaises(config.ConfigError):
            config.resolve("runtime", {"AGENTSWE_DEFAULT_API_KEY": "k", "AGENTSWE_RUNTIME_WIRE": "grpc"})
        with self.assertRaises(config.ConfigError):
            config.resolve("runtime", {"AGENTSWE_DEFAULT_API_KEY": "k",
                                       "AGENTSWE_DEFAULT_BASE_URL": "https://user:pw@a/v1"})

    def test_describe_never_contains_a_key(self):
        env = {"AGENTSWE_DEFAULT_BASE_URL": "https://a/v1", "AGENTSWE_DEFAULT_API_KEY": FAKE_KEY}
        blob = json.dumps(config.describe_all(env))
        self.assertNotIn(FAKE_KEY, blob)
        self.assertIn('"api_key_set": true', blob)

    def test_credential_file_is_0600_and_exact(self):
        work = tmpdir()
        path = config.write_credential_file(work / "k.env", "AGENTSWE_JUDGE_API_KEY", "v")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(config.read_credential(path, "AGENTSWE_JUDGE_API_KEY"), "v")
        with self.assertRaises(config.ConfigError):
            config.read_credential(path, "AGENTSWE_DEFAULT_API_KEY")
        with self.assertRaises(FileExistsError):
            config.write_credential_file(path, "X", "y")

    def test_dotenv_parsing(self):
        work = tmpdir()
        (work / ".env").write_text("# c\nexport AGENTSWE_JUDGE_MODEL='m1'\nAGENTSWE_JUDGE_EFFORT=\"high\"\n")
        env = config.environment(work / ".env", base={"AGENTSWE_JUDGE_MODEL": "from-process",
                                                       "AGENTSWE_DEFAULT_API_KEY": "k"})
        cfg = config.resolve("judge", env)
        self.assertEqual((cfg.model, cfg.effort), ("from-process", "high"))


class CommandLine(unittest.TestCase):
    def test_serve_ready_line_and_describe_have_no_key(self):
        provider = FakeProvider()
        work = tmpdir()
        envfile = work / ".env"
        envfile.write_text("AGENTSWE_DEFAULT_BASE_URL=%s\nAGENTSWE_DEFAULT_API_KEY=%s\n" % (provider.base, FAKE_KEY))
        os.chmod(envfile, 0o600)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        for name in list(env):
            if name.startswith("AGENTSWE_"):
                env.pop(name)
        described = subprocess.run([sys.executable, "-m", "agentswe_broker", "describe", "--env-file", str(envfile)],
                                   capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(described.returncode, 0, described.stderr)
        self.assertNotIn(FAKE_KEY, described.stdout + described.stderr)
        process = subprocess.Popen([sys.executable, "-m", "agentswe_broker", "serve", "--role", "runtime",
                                    "--env-file", str(envfile), "--stats-file", str(work / "s.json"),
                                    "--ledger-dir", str(work / "l"), "--port", "0"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            ready = json.loads(process.stdout.readline())
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["key_source"], "AGENTSWE_DEFAULT_API_KEY")
            from fakes import responses_completed
            provider.add(Script(200, json.dumps(responses_completed("x")).encode()))
            request = urllib.request.Request("http://127.0.0.1:%d/v1/responses" % ready["port"],
                                             data=b'{"input":"q"}', method="POST",
                                             headers={"Authorization": "Bearer runtime-only-placeholder",
                                                      "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
        finally:
            process.terminate()
            out, err = process.communicate(timeout=10)
            provider.close()
        self.assertNotIn(FAKE_KEY, json.dumps(ready) + out + err)
        assert_no_key(self, work / "l", work / "s.json")


if __name__ == "__main__":
    unittest.main()
