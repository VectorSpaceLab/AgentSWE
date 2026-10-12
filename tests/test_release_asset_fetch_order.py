"""Release assets: the store is tried before upstream URLs, and an upstream that delivers no byte before timing out
is left for the next source at once instead of being resumed 30 times (stdlib unittest, no network)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import optimization_assets as oa  # noqa: E402


class Cfg:
    def __init__(self, home: Path, url: str | None):
        self.home, self.url = home, url

    def get(self, key, default=None):
        return {"AGENTSWE_RELEASE_ASSETS_URL": self.url}.get(key, default)


class Order(unittest.TestCase):
    def test_store_first_then_upstream(self):
        seen = []

        def fake_cached(cfg, basename, sha256, urls, size=None):
            seen.append(list(urls))
            raise SystemExit("stop")

        item = {"path": "fixture-downloads/a.docx", "sha256": "ab" * 32, "size": 3,
                "urls": ["https://huggingface.co/x/a.docx"], "release_path": "osworld/fixture-downloads/a.docx"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(oa, "_cached", fake_cached):
            with self.assertRaises(SystemExit):
                oa.fetch_assets(Cfg(Path(tmp), "https://example.org/store"), {"root": "assets", "files": [item]})
        self.assertEqual(seen[0], ["https://example.org/store/osworld/fixture-downloads/a.docx",
                                   "https://huggingface.co/x/a.docx"])

    def test_upstream_only_without_a_store(self):
        seen = []

        def fake_cached(cfg, basename, sha256, urls, size=None):
            seen.append(list(urls))
            raise SystemExit("stop")

        item = {"path": "a.img", "sha256": "cd" * 32, "urls": ["https://archive.org/a.img"]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(oa, "_cached", fake_cached):
            with self.assertRaises(SystemExit):
                oa.fetch_assets(Cfg(Path(tmp), None), {"root": "assets", "files": [item]})
        self.assertEqual(seen[0], ["https://archive.org/a.img"])


class Unreachable(unittest.TestCase):
    def test_timeout_without_bytes_moves_on(self):
        calls = []

        def run(cmd, env=None, check=False):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 28)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(oa.subprocess, "run", run):
            ok = oa._fetch(Cfg(Path(tmp), None), "https://unreachable.example/x", Path(tmp) / "x")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_timeout_after_bytes_resumes(self):
        calls = []

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "x"
            partial = dest.with_name("x.part")

            def run(cmd, env=None, check=False):
                calls.append(cmd)
                if len(calls) == 1:
                    partial.write_bytes(b"half")
                    return subprocess.CompletedProcess(cmd, 28)
                partial.write_bytes(b"halfdone")
                return subprocess.CompletedProcess(cmd, 0)

            with mock.patch.object(oa.subprocess, "run", run):
                ok = oa._fetch(Cfg(Path(tmp), None), "https://slow.example/x", dest)
            self.assertTrue(ok)
            self.assertEqual(dest.read_bytes(), b"halfdone")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
