"""Editing environment archives come from AGENTSWE_ENV_ARCHIVE_DIR or, when that does not hold them, from the release
asset store (AGENTSWE_RELEASE_ASSETS_URL/editing/env-archives/<name>), sha256-checked (stdlib unittest; a local HTTP
server stands in for the store)."""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402


class Cfg:
    def __init__(self, home: Path, values: dict[str, str]):
        self.home, self.values = home, values

    def get(self, key, default=None):
        return self.values.get(key, default)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PinnedArchives(unittest.TestCase):
    def test_release_env_specs(self):
        names = {}
        for task in ("openclaw-channel-handoff", "codex-execution-residual", "dyad-acceptance-driven"):
            meta = json.loads((ROOT / "tasks" / "editing" / task / "env" / "env.json").read_text())
            names[task] = [(n, opt) for n, _, _, opt in ed.pinned_env_archives(meta)]
        self.assertEqual(names["openclaw-channel-handoff"],
                         [("openclaw-channel-handoff-ledger-edit-v1.runtime.tar.zst", False),
                          ("openclaw-channel-handoff-ledger-edit-v1.runtime.manifest.jsonl", False)])
        self.assertEqual(names["codex-execution-residual"], [("codex-project-memory-edit-v1.cargo-home.tar.zst", True)])
        self.assertEqual(names["dyad-acceptance-driven"], [("dyad-cycle-006-deps.tar.zst", False)])


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home, self.store = base / "home", base / "store"
        (self.store / ed.ENV_ARCHIVE_RELEASE_DIR).mkdir(parents=True)
        self.files = {"env-a.tar.zst": b"archive bytes", "env-a.manifest.jsonl": b"{}\n", "opt.tar.zst": b"optional"}
        for name, data in self.files.items():
            if name != "opt.tar.zst":
                (self.store / ed.ENV_ARCHIVE_RELEASE_DIR / name).write_bytes(data)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.store))
        handler.log_message = lambda *a, **k: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.meta = {"archive": {"name": "env-a.tar.zst", "sha256": sha(self.files["env-a.tar.zst"]),
                                 "size": len(self.files["env-a.tar.zst"]), "manifest": "env-a.manifest.jsonl",
                                 "manifest_sha256": sha(self.files["env-a.manifest.jsonl"])},
                     "archives": [{"name": "opt.tar.zst", "sha256": sha(b"optional"), "optional": True}]}
        self.saved = {k: os.environ.pop(k, None) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v

    def test_fetches_required_and_skips_missing_optional(self):
        stage = ed.stage_env_archives(Cfg(self.home, {"AGENTSWE_RELEASE_ASSETS_URL": self.url + "/"}), self.meta)
        self.assertEqual(stage, self.home / "cache" / "env-archives")
        self.assertEqual((stage / "env-a.tar.zst").read_bytes(), self.files["env-a.tar.zst"])
        self.assertEqual((stage / "env-a.manifest.jsonl").read_bytes(), self.files["env-a.manifest.jsonl"])
        self.assertFalse((stage / "opt.tar.zst").exists())

    def test_wrong_bytes_in_the_store_stop_setup(self):
        (self.store / ed.ENV_ARCHIVE_RELEASE_DIR / "env-a.tar.zst").write_bytes(b"tampered")
        with self.assertRaises(SystemExit):
            ed.stage_env_archives(Cfg(self.home, {"AGENTSWE_RELEASE_ASSETS_URL": self.url}), self.meta)

    def test_operator_directory_with_every_archive_is_used_as_is(self):
        local = Path(self.tmp.name) / "local"
        local.mkdir()
        for name, data in self.files.items():
            (local / name).write_bytes(data)
        cfg = Cfg(self.home, {"AGENTSWE_ENV_ARCHIVE_DIR": str(local), "AGENTSWE_RELEASE_ASSETS_URL": self.url})
        self.assertIsNone(ed.stage_env_archives(cfg, self.meta))

    def test_without_a_store_build_host_decides(self):
        self.assertIsNone(ed.stage_env_archives(Cfg(self.home, {}), self.meta))


if __name__ == "__main__":
    unittest.main()
