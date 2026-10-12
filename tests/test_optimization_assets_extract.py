"""Re-running setup does not replace an extracted tar tree that only gained editable-install metadata (stdlib).

    python3 -m unittest tests/test_optimization_assets_extract.py

OSWorld's osworld-source is unpacked from a pinned tar and then installed editable into the OSWorld venv, which writes
osworld.egg-info into the tree and so changes the directory mtime the extraction marker records. Setup used to treat
that as a stale tree and re-extract it (rmtree, then unpack), under any OSWorld run active at the time. Now the tree is
re-verified against the pinned digest without install metadata, and only the marker is refreshed; a real content change
still re-extracts.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentswe.runners import optimization_assets as oa  # noqa: E402


def make_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 0
            tf.addfile(info, io.BytesIO(data))


class TarExtractMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.archive, self.root = base / "src.tar", base / "assets"
        files = {"pkg/setup.py": b"from setuptools import setup\n", "pkg/mod/__init__.py": b"X = 1\n"}
        make_tar(self.archive, files)
        stage = base / "stage"
        with tarfile.open(self.archive) as tf:
            tf.extractall(stage)
        self.spec = {"kind": "tar", "dest": "src", "tree_sha256": oa.tree_digest(stage)}
        oa._extract(self.archive, self.root, self.spec)
        self.dest = self.root / "src"

    def tearDown(self):
        self.tmp.cleanup()

    def extract_counting(self) -> int:
        calls = []
        real = tarfile.open
        with mock.patch.object(oa.tarfile, "open", side_effect=lambda *a, **k: calls.append(a) or real(*a, **k)):
            oa._extract(self.archive, self.root, self.spec)
        return len(calls)

    def test_install_metadata_keeps_the_tree(self):
        sentinel = self.dest / "pkg" / "setup.py"
        inode = sentinel.stat().st_ino
        meta = self.dest / "pkg" / "osworld.egg-info"
        meta.mkdir()
        (meta / "PKG-INFO").write_text("Metadata-Version: 2.1\n")
        os.utime(self.dest, ns=(1, 1))  # the marker's recorded mtime no longer matches
        self.assertEqual(self.extract_counting(), 0)
        self.assertEqual(sentinel.stat().st_ino, inode)  # same file: not removed and re-unpacked
        self.assertTrue((meta / "PKG-INFO").is_file())
        marker = json.loads((self.root / ".src.verified.json").read_text())
        self.assertEqual(marker["mtime_ns"], self.dest.stat().st_mtime_ns)  # refreshed: next run takes the fast path
        self.assertEqual(self.extract_counting(), 0)

    def test_changed_content_is_re_extracted(self):
        (self.dest / "pkg" / "mod" / "__init__.py").write_text("X = 2\n")
        os.utime(self.dest, ns=(1, 1))
        self.assertEqual(self.extract_counting(), 1)
        self.assertEqual((self.dest / "pkg" / "mod" / "__init__.py").read_text(), "X = 1\n")

    def test_metadata_only_matters_for_the_lenient_digest(self):
        (self.dest / "x.egg-info").mkdir()
        (self.dest / "x.egg-info" / "PKG-INFO").write_text("m\n")
        self.assertNotEqual(oa.tree_digest(self.dest), self.spec["tree_sha256"])
        self.assertEqual(oa.tree_digest(self.dest, ignore_install_metadata=True), self.spec["tree_sha256"])


if __name__ == "__main__":
    unittest.main()
