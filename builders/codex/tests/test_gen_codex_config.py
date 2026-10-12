"""Tests for the Codex 0.144.1 config/catalog generator (offline, stdlib only)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import gen_codex_config as G  # noqa: E402


def run(*argv):
    out = Path(tempfile.mkdtemp(prefix="oss-op-codex-"))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = G.main(list(argv) + ["--out-dir", str(out)])
    return code, out


class Generate(unittest.TestCase):
    def manifest(self, out):
        return json.loads((out / "codex_config_manifest.json").read_text())

    def test_lite_models_reproduce_the_lite_catalog(self):
        for model, effort, wire in (("deepseek-flash", "max", "responses"), ("gpt-5.6-sol", "xhigh", "responses"),
                                    ("Qwen/Qwen3.6-35B-A3B", "max", "chat")):
            code, out = run("--model", model, "--effort", effort, "--wire", wire,
                            "--base-url", "http://172.17.0.1:18114/v1")
            self.assertEqual(code, 0, model)
            manifest = self.manifest(out)
            self.assertTrue(manifest["lite_identical"], model)
            self.assertEqual(manifest["catalog_sha256"], G.LITE_CATALOG_SHA256)
            self.assertEqual(G.verify((out / "config.toml").read_text(),
                                      json.loads((out / "model_catalog.json").read_text())), [])

    def test_both_multi_agent_switches_present(self):
        _, out = run("--model", "deepseek-flash", "--effort", "max", "--base-url", "http://b/v1")
        config = G.parse_simple_toml((out / "config.toml").read_text())
        catalog = json.loads((out / "model_catalog.json").read_text())
        entry = [m for m in catalog["models"] if m["slug"] == "deepseek-flash"][0]
        self.assertIs(config["features"]["multi_agent"], False)
        self.assertIsNone(entry["multi_agent_version"])
        self.assertEqual(entry["apply_patch_tool_type"], "freeform")
        self.assertEqual(config["model_catalog_json"], G.CONTAINER_CATALOG)
        self.assertEqual(config["model_providers"]["agentswe"]["env_key"], G.BROKER_ENV_KEY)
        self.assertEqual(config["model_providers"]["agentswe"]["wire_api"], "responses")

    def test_unknown_slug_needs_a_context_window(self):
        code, _ = run("--model", "acme/coder-9b", "--effort", "high", "--base-url", "http://b/v1")
        self.assertEqual(code, 2)
        code, out = run("--model", "acme/coder-9b", "--effort", "high", "--context-window", "120000",
                        "--base-url", "http://b/v1")
        self.assertEqual(code, 0)
        manifest = self.manifest(out)
        self.assertEqual(manifest["cloned_entries"], ["acme/coder-9b", "coder-9b"])
        self.assertFalse(manifest["lite_identical"])
        catalog = json.loads((out / "model_catalog.json").read_text())
        clone = [m for m in catalog["models"] if m["slug"] == "acme/coder-9b"][0]
        self.assertEqual((clone["context_window"], clone["multi_agent_version"], clone["tool_mode"]),
                         (120000, None, None))

    def test_builtin_entry_with_multi_agent_is_nulled_and_recorded(self):
        code, out = run("--model", "gpt-5.6-terra", "--effort", "high", "--base-url", "http://b/v1")
        self.assertEqual(code, 0)
        manifest = self.manifest(out)
        self.assertEqual(manifest["modified_fields"], ["gpt-5.6-terra.multi_agent_version"])
        self.assertFalse(manifest["lite_identical"])

    def test_effort_must_be_a_supported_level(self):
        code, _ = run("--model", "deepseek-flash", "--effort", "xhigh", "--base-url", "http://b/v1")
        self.assertEqual(code, 2)

    def test_chat_wire_requires_the_broker(self):
        code, _ = run("--model", "Qwen/Qwen3.6-35B-A3B", "--effort", "max", "--wire", "chat",
                      "--provider-type", "direct", "--auth-command", "/x", "--base-url", "https://p/v1")
        self.assertEqual(code, 2)

    def test_direct_mode_uses_an_auth_command_never_a_key(self):
        code, _ = run("--model", "deepseek-flash", "--effort", "max", "--provider-type", "direct",
                      "--base-url", "https://api.deepseek.com/v1")
        self.assertEqual(code, 2)
        code, out = run("--model", "deepseek-flash", "--effort", "max", "--provider-type", "direct",
                        "--auth-command", "/tmp/codex-secrets/provider-auth.sh",
                        "--base-url", "https://api.deepseek.com/v1")
        self.assertEqual(code, 0)
        config = G.parse_simple_toml((out / "config.toml").read_text())
        provider = config["model_providers"]["agentswe"]
        self.assertNotIn("env_key", provider)
        self.assertEqual(provider["auth"]["command"], "/tmp/codex-secrets/provider-auth.sh")

    def test_retry_keys_only_when_requested(self):
        _, out = run("--model", "deepseek-flash", "--effort", "max", "--base-url", "http://b/v1",
                     "--request-max-retries", "10", "--stream-max-retries", "10",
                     "--stream-idle-timeout-ms", "300000")
        provider = G.parse_simple_toml((out / "config.toml").read_text())["model_providers"]["agentswe"]
        self.assertEqual((provider["request_max_retries"], provider["stream_max_retries"],
                          provider["stream_idle_timeout_ms"]), (10, 10, 300000))


class Verify(unittest.TestCase):
    def setUp(self):
        _, self.out = run("--model", "deepseek-flash", "--effort", "max", "--base-url", "http://b/v1")
        self.config = (self.out / "config.toml").read_text()
        self.catalog = json.loads((self.out / "model_catalog.json").read_text())

    def test_missing_features_switch(self):
        broken = self.config.replace("[features]\nmulti_agent = false\n", "")
        self.assertIn("[features] multi_agent = false is missing", G.verify(broken, self.catalog))

    def test_catalog_switch(self):
        catalog = json.loads(json.dumps(self.catalog))
        for entry in catalog["models"]:
            if entry["slug"] == "deepseek-flash":
                entry["multi_agent_version"] = "v2"
        self.assertTrue(any("multi_agent_version" in e for e in G.verify(self.config, catalog)))

    def test_fallback_metadata(self):
        broken = self.config.replace('model_catalog_json = "/tmp/codex-home/model_catalog.json"\n', "")
        self.assertTrue(any("fallback metadata" in e for e in G.verify(broken, self.catalog)))

    def test_chat_wire_rejected(self):
        broken = self.config.replace('wire_api = "responses"', 'wire_api = "chat"')
        self.assertTrue(any("wire_api" in e for e in G.verify(broken, self.catalog)))

    def test_literal_credentials_rejected(self):
        broken = self.config + 'experimental_bearer_token = "sk-abcdefghijklmnopqrstu"\n'
        self.assertTrue(any("credential" in e for e in G.verify(broken, self.catalog)))

    def test_cli_verify(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(G.main(["--verify", str(self.out)]), 0)


if __name__ == "__main__":
    unittest.main()
