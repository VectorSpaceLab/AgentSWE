"""Search front trust mounts in the Candidate compose of both search-enabled Creation tasks (Web, PPTX).

    python3 -m unittest tests/test_search_front_trust.py      (standard library only; no docker, no network)

The per-run bundle must be bind-mounted read-only over the default trust stores a Candidate can use (system bundle,
OpenSSL capath entry, every certifi cacert.pem and the conda OpenSSL cafile of each mounted env prefix, under every
alias of that prefix), only over files that already exist in the read-only prefix, and each staging is recorded.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "runners" / "creation"
sys.path.insert(0, str(SHARED))
import candidate_broker_protocol as protocol  # noqa: E402
import runtime_contract  # noqa: E402


def load_adapter(task: str):
    os.environ.setdefault("AGENTSWE_CREATION_SHARED", str(SHARED))
    spec = importlib.util.spec_from_file_location(f"adapter_{task.replace('-', '_')}",
                                                  ROOT / "tasks" / "creation" / task / "adapter" / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_prefix(root: Path, documented: str) -> Path:
    """A conda-like prefix: certifi (also under pip/_vendor), ssl/cert.pem -> cacert.pem, lib/python3.1 symlink,
    a pip shebang naming the documented prefix, and a certifi symlink that leaves the prefix (must be ignored)."""
    site = root / "lib" / "python3.11" / "site-packages"
    for rel in ("certifi/cacert.pem", "pip/_vendor/certifi/cacert.pem"):
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text("roots\n")
    (root / "lib" / "python3.1").symlink_to("python3.11")
    (root / "ssl").mkdir()
    (root / "ssl" / "cacert.pem").write_text("roots\n")
    (root / "ssl" / "cert.pem").symlink_to("cacert.pem")
    (root / "bin").mkdir()
    (root / "bin" / "pip").write_text(f"#!{documented}/bin/python3.11\n")
    outside = root.parent / "outside-certifi"
    outside.mkdir(exist_ok=True)
    (outside / "cacert.pem").write_text("roots\n")
    (site / "vendored").mkdir()
    (site / "vendored" / "certifi").symlink_to(outside)
    return root


class SearchFrontTrust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        for name in ("ca-bundle.pem", "ca.pem", "cert.pem", "key.pem", "search_front.py", "judge.env"):
            (self.work / name).write_text(name + "\n")
        self.record = self.work / "search_trust.jsonl"
        self.spec = {"image": "front", "broker_host": "172.17.0.1", "broker_port": 40000,
                     "script": str(self.work / "search_front.py"), "cert": str(self.work / "cert.pem"),
                     "key": str(self.work / "key.pem"), "ca_bundle": str(self.work / "ca-bundle.pem"),
                     "ca": str(self.work / "ca.pem"), "ca_subject_hash": "0123abcd", "trust_record": str(self.record)}
        self.saved = os.environ.get("AGENTSWE_SEARCH_FRONT")
        os.environ["AGENTSWE_SEARCH_FRONT"] = json.dumps(self.spec)
        os.environ["AGENTSWE_CANDIDATE_CREDENTIAL_FILE"] = str(self.work / "judge.env")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("AGENTSWE_SEARCH_FRONT", None)
        else:
            os.environ["AGENTSWE_SEARCH_FRONT"] = self.saved
        self.tmp.cleanup()

    def check(self, config: dict, prefix: Path, target: str) -> None:
        compose = self.work / "stage" / "environment" / "docker-compose.yaml"
        compose.parent.mkdir(parents=True, exist_ok=True)
        config = runtime_contract.prepare_compose(compose, config)  # as staging writes it (adds prefix aliases)
        volumes = config["services"]["main"]["volumes"]
        by_target = {v["target"]: v for v in volumes}
        self.assertEqual(len(by_target), len(volumes), "duplicate mount targets")
        bundle = self.spec["ca_bundle"]
        self.assertEqual(by_target["/etc/ssl/certs/ca-certificates.crt"]["source"], bundle)
        self.assertEqual(by_target["/etc/ssl/certs/0123abcd.0"]["source"], self.spec["ca"])
        aliases = sorted(runtime_contract.environment_aliases(prefix, target))
        self.assertIn(target.replace("/agent-create-0804/envs/", "/envs/"), aliases)
        prefix_mounts = [t for t in aliases if t in by_target]
        self.assertEqual(prefix_mounts, aliases, "prefix aliases mounted by prepare_compose")
        rels = ["lib/python3.11/site-packages/certifi/cacert.pem",
                "lib/python3.11/site-packages/pip/_vendor/certifi/cacert.pem", "ssl/cacert.pem"]
        for alias in aliases:
            for rel in rels:
                mount = by_target.get(f"{alias}/{rel}")
                self.assertIsNotNone(mount, f"{alias}/{rel}")
                self.assertEqual((mount["source"], mount["read_only"]), (bundle, True))
        trust = [v for v in volumes if v["source"] in (bundle, self.spec["ca"])]
        self.assertTrue(all(v["read_only"] is True and v["type"] == "bind" for v in trust))
        for v in trust:  # every prefix target already exists in the prefix: nothing is created there
            for alias in aliases:
                if v["target"].startswith(alias + "/"):
                    self.assertTrue((prefix / v["target"][len(alias) + 1:]).is_file(), v["target"])
        self.assertFalse(any("vendored" in v["target"] or "python3.1/" in v["target"] for v in trust))
        env = config["services"]["main"]["environment"]
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
            self.assertEqual(env[name], protocol.SEARCH_TRUST_BUNDLE)
        self.assertIn("agentswe-search-front", config["services"])
        rows = [json.loads(line) for line in self.record.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]["targets"]),
                         sorted(v["target"] for v in trust if v["target"] != protocol.SEARCH_TRUST_BUNDLE))
        self.assertEqual(rows[0]["env_prefixes"][0]["files"], sorted(rels))

    def test_web_compose(self):
        web = load_adapter("web-research-report")
        target = str(web.CONTAINER_ENV_PREFIX)
        prefix = fake_prefix(self.work / "web-env", target)
        (self.work / "case" / "dev_001").mkdir(parents=True)
        config = web.compose_config(self.work, self.work / "case" / "dev_001", prefix, self.work / "judge.env")
        self.check(config, prefix, target)

    def test_pptx_compose(self):
        pptx = load_adapter("document-to-editable-pptx")
        target = str(pptx.CONTAINER_ENV_PREFIX)
        prefix = fake_prefix(self.work / "pptx-env", target)
        # compose_config itself needs a real PPTX runtime (browser, pdftoppm); the mounts it hands to
        # configure_candidate_compose are rebuilt here from the adapter's own constants.
        bind = lambda s, t: {"type": "bind", "source": str(s), "target": str(t), "read_only": True}  # noqa: E731
        config = protocol.configure_candidate_compose({"services": {"main": {"volumes": [
            bind(self.work, "/submission"), bind(self.work, "/active-case/dev_001"), bind(prefix, target),
            bind(self.work / "judge.env", pptx.CONTAINER_CANDIDATE_CREDENTIAL_FILE)], "environment": {}}}})
        self.check(config, prefix, target)

    def test_no_front_no_mounts(self):
        os.environ.pop("AGENTSWE_SEARCH_FRONT")
        services = {"main": {"volumes": []}}
        protocol.add_search_front(services, services["main"])
        self.assertEqual(services, {"main": {"volumes": []}})

    def test_system_target_inside_existing_mount_refused(self):
        main = {"volumes": [{"type": "bind", "source": str(self.work), "target": "/etc/ssl", "read_only": True}]}
        with self.assertRaises(RuntimeError):
            protocol.add_search_front({"main": main}, main)


if __name__ == "__main__":
    unittest.main()
