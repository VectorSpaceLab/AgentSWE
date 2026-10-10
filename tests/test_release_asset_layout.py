"""Flat release-asset layout (a GitHub release): every pinned file is published as
<sha256[:16]>__<file name, characters outside [A-Za-z0-9._-] replaced by _>, and both fetchers (Optimization task
assets, Editing environment archives) request that name when AGENTSWE_RELEASE_ASSETS_URL is a GitHub release download
URL or AGENTSWE_RELEASE_ASSETS_LAYOUT=flat; tools/release_assets.py builds the upload set (stdlib unittest; a local HTTP
server serving .../releases/download/<tag>/ stands in for the release)."""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402
from agentswe.runners import optimization_assets as oa  # noqa: E402
import release_assets as ra  # noqa: E402

GH = "https://github.com/example-org/example-repo/releases/download/assets-v1"
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


class Cfg:
    def __init__(self, home: Path | None, values: dict[str, str]):
        self.home, self.values = home, values

    def get(self, key, default=None):
        return self.values.get(key) or default


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Names(unittest.TestCase):
    def test_flat_name_is_the_cache_name_with_unsafe_characters_replaced(self):
        s = "ab" * 32
        self.assertEqual(oa.release_asset_name(s, "uv-0.12.1"), oa.cache_name(s, "uv-0.12.1"))
        self.assertEqual(oa.release_asset_name(s, "04 CHIN9505 EBook (1).docx"),
                         "abababababababab__04_CHIN9505_EBook__1_.docx")
        self.assertEqual(oa.release_asset_name(s, "na" + chr(0xEF) + "ve#1+2~.tar.gz"), "abababababababab__na_ve_1_2_.tar.gz")

    def test_layout_follows_the_url_unless_set(self):
        def layout(**values):
            return oa.release_layout(Cfg(None, values))
        url = "AGENTSWE_RELEASE_ASSETS_URL"
        for flat in (GH, GH + "/", "https://github.com/o/r/releases/latest/download", "http://127.0.0.1:8000/x/releases/download/t"):
            self.assertEqual(layout(**{url: flat}), "flat", flat)
        for tree in ("https://assets.example.org/agentswe", "https://github.com/o/r/releases", ""):
            self.assertEqual(layout(**{url: tree}), "tree", tree)
        self.assertEqual(layout(**{url: GH, "AGENTSWE_RELEASE_ASSETS_LAYOUT": "tree"}), "tree")
        self.assertEqual(layout(**{url: "https://assets.example.org", "AGENTSWE_RELEASE_ASSETS_LAYOUT": " Flat "}), "flat")
        with self.assertRaises(SystemExit):
            layout(**{url: GH, "AGENTSWE_RELEASE_ASSETS_LAYOUT": "nested"})

    def test_urls(self):
        rel, s = "osworld/fixture-downloads/abc_04 Purchasing info.docx", "cd" * 32
        self.assertIsNone(oa.release_asset_url(Cfg(None, {}), rel, s))
        self.assertEqual(oa.release_asset_url(Cfg(None, {"AGENTSWE_RELEASE_ASSETS_URL": "https://a.example.org/s/"}), rel, s),
                         "https://a.example.org/s/osworld/fixture-downloads/abc_04%20Purchasing%20info.docx")
        self.assertEqual(oa.release_asset_url(Cfg(None, {"AGENTSWE_RELEASE_ASSETS_URL": GH + "/"}), rel, s),
                         f"{GH}/cdcdcdcdcdcdcdcd__abc_04_Purchasing_info.docx")


class RealPins(unittest.TestCase):
    def test_every_pinned_file_has_its_own_plain_flat_name(self):
        pins = ra.pins()
        # the staged tree store (SHA256SUMS: 84 files): 4 Editing archives, 71 OSWorld files, 9 Terminal-Bench files
        self.assertEqual(len(pins), 84)
        self.assertEqual(sum(p.release_path.startswith("editing/env-archives/") for p in pins), 4)
        names = [p.name for p in pins]
        self.assertEqual(len(set(names)), len(pins))
        self.assertEqual(len({n.lower() for n in names}), len(pins))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{16}__[A-Za-z0-9._-]+", n) for n in names))
        self.assertTrue(not {n.lower() for n in names} & {n.lower() for n in ra.EXTRA})
        self.assertEqual(len(ra.flat_assets(pins)), 84)
        self.assertEqual(sum(" " in p.release_path for p in pins), 6)

    def test_shared_pins_are_one_release_path(self):
        uv = [p for p in ra.pins() if p.release_path == "terminalbench/task-assets/common/uv-0.12.1"]
        self.assertEqual([p.tasks for p in uv], [("osworld", "terminalbench")])


class Collisions(unittest.TestCase):
    def test_different_bytes_under_one_name_are_refused(self):
        a = ra.Pin("x/a b.txt", "0" * 16 + "1" * 48, 1, ("t",))
        b = ra.Pin("y/a_b.txt", "0" * 16 + "2" * 48, 1, ("t",))
        with self.assertRaises(SystemExit):
            ra.flat_assets([a, b])

    def test_identical_bytes_share_one_asset(self):
        s = "3" * 64
        groups = ra.flat_assets([ra.Pin("x/a b.txt", s, 1, ("t",)), ra.Pin("y/a_b.txt", s, 1, ("u",))])
        self.assertEqual(list(groups), ["3333333333333333__a_b.txt"])
        self.assertEqual(len(groups["3333333333333333__a_b.txt"]), 2)

    def test_names_that_differ_only_by_case_are_refused(self):
        s = "4" * 64
        with self.assertRaises(SystemExit):
            ra.flat_assets([ra.Pin("x/A.txt", s, 1, ("t",)), ra.Pin("y/a.txt", s, 1, ("t",))])


class FlatStore(unittest.TestCase):
    """Both fetch paths against a local stand-in for a GitHub release (no layout setting: the URL selects flat)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home, serve = base / "home", base / "serve"
        self.release = serve / "example-org" / "example-repo" / "releases" / "download" / "assets-v1"
        self.release.mkdir(parents=True)
        self.requests: list[str] = []
        requests = self.requests

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                requests.append(self.path)

        handler = functools.partial(Handler, directory=str(serve))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/example-org/example-repo/releases/download/assets-v1"
        self.saved = {k: os.environ.pop(k, None) for k in PROXY_VARS}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v

    def publish(self, release_path: str, data: bytes) -> str:
        name = oa.release_asset_name(sha(data), release_path.rsplit("/", 1)[-1])
        (self.release / name).write_bytes(data)
        return name

    def test_task_assets_and_env_archives_are_fetched_by_flat_name(self):
        doc, archive, manifest = b"docx bytes", b"archive bytes", b"{}\n"
        rel = "osworld/fixture-downloads/abc_04 Purchasing info 2021 Jan.docx"
        names = {self.publish(rel, doc), self.publish("editing/env-archives/env-a.tar.zst", archive),
                 self.publish("editing/env-archives/env-a.manifest.jsonl", manifest)}
        cfg = Cfg(self.home, {"AGENTSWE_RELEASE_ASSETS_URL": self.url})
        spec = {"root": "assets/osworld", "files": [{"path": "fixture-downloads/abc_04 Purchasing info 2021 Jan.docx",
                                                    "sha256": sha(doc), "size": len(doc), "urls": [], "release_path": rel}]}
        self.assertEqual(len(oa.fetch_assets(cfg, spec)), 1)
        self.assertEqual((self.home / "assets/osworld/fixture-downloads/abc_04 Purchasing info 2021 Jan.docx").read_bytes(), doc)
        meta = {"archive": {"name": "env-a.tar.zst", "sha256": sha(archive), "size": len(archive),
                            "manifest": "env-a.manifest.jsonl", "manifest_sha256": sha(manifest)}}
        stage = ed.stage_env_archives(cfg, meta)
        self.assertEqual((stage / "env-a.tar.zst").read_bytes(), archive)
        self.assertEqual((stage / "env-a.manifest.jsonl").read_bytes(), manifest)
        prefix = "/example-org/example-repo/releases/download/assets-v1/"
        self.assertEqual({p[len(prefix):] for p in self.requests if p.startswith(prefix)}, names)

    def test_layout_setting_selects_flat_names_on_any_url(self):
        data = b"tar bytes"
        name = self.publish("editing/env-archives/env-b.tar.zst", data)
        mirror = self.release.parent.parent.parent.parent.parent / "mirror"
        mirror.symlink_to(self.release, target_is_directory=True)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/mirror"
        cfg = Cfg(self.home, {"AGENTSWE_RELEASE_ASSETS_URL": url, "AGENTSWE_RELEASE_ASSETS_LAYOUT": "flat"})
        stage = ed.stage_env_archives(cfg, {"archives": [{"name": "env-b.tar.zst", "sha256": sha(data)}]})
        self.assertEqual((stage / "env-b.tar.zst").read_bytes(), data)
        self.assertEqual(self.requests, [f"/mirror/{name}"])


class UploadSet(unittest.TestCase):
    """tools/release_assets.py build/verify on a miniature checkout and tree store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo, self.store, self.out = base / "repo", base / "store", base / "flat"
        self.files = {"osworld/fixture-downloads/u_A file (1).docx": b"doc", "terminalbench/task-assets/x/y.tar.gz": b"tgz",
                      "editing/env-archives/env-c.tar.zst": b"zst"}
        for rel, data in self.files.items():
            (self.store / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.store / rel).write_bytes(data)
        task = {"id": "mini", "runner_config": {"assets": {"root": "assets/mini", "files": [
            {"path": rel.split("/", 1)[1], "sha256": sha(d), "size": len(d), "release_path": rel}
            for rel, d in self.files.items() if not rel.startswith("editing/")]}}}
        (self.repo / "tasks/optimization/mini").mkdir(parents=True)
        (self.repo / "tasks/optimization/mini/task.json").write_text(json.dumps(task))
        (self.repo / "tasks/editing/ed/env").mkdir(parents=True)
        (self.repo / "tasks/editing/ed/env/env.json").write_text(json.dumps(
            {"archives": [{"name": "env-c.tar.zst", "sha256": sha(b"zst"), "optional": True}]}))
        for rel in ra.DOCS.values():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(f"{rel}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_writes_exactly_the_upload_set(self):
        r = ra.build(self.store, self.out, root=self.repo)
        self.assertEqual((r["pinned"], r["assets"]), (3, 9))
        names = {oa.release_asset_name(sha(d), rel.rsplit("/", 1)[-1]) for rel, d in self.files.items()}
        self.assertEqual({p.name for p in self.out.iterdir()}, names | set(ra.EXTRA))
        self.assertIn(f"{sha(b'doc')[:16]}__u_A_file__1_.docx", names)
        manifest = json.loads((self.out / ra.MANIFEST).read_text())
        rows = {a["name"]: a for a in manifest["assets"]}
        self.assertEqual(rows[f"{sha(b'doc')[:16]}__u_A_file__1_.docx"]["release_paths"],
                         ["osworld/fixture-downloads/u_A file (1).docx"])
        sums = dict(line.split("  ")[::-1] for line in (self.out / ra.SUMS).read_text().splitlines())
        self.assertEqual(len(sums), 3 + 1 + len(ra.DOCS))
        self.assertTrue(all(sha((self.out / n).read_bytes()) == s for n, s in sums.items()))
        self.assertEqual(ra.verify(self.out, root=self.repo), 9)
        self.assertEqual({rel: (self.store / rel).read_bytes() for rel in self.files}, self.files)
        self.assertEqual(ra.build(self.store, self.out, root=self.repo)["kept"], 3)

    def test_repo_named_in_the_readme_only(self):
        ra.build(self.store, self.out, root=self.repo)
        self.assertIn("https://github.com/<owner>/<repo>/releases/download/assets-v1", (self.out / ra.README).read_text())
        pinned = {p.name: p.read_bytes() for p in self.out.iterdir() if "__" in p.name}
        r = ra.build(self.store, self.out, root=self.repo, repo="example-org/example-repo")
        self.assertEqual(r["kept"], 3)
        self.assertIn("AGENTSWE_RELEASE_ASSETS_URL=https://github.com/example-org/example-repo/releases/download/assets-v1",
                      (self.out / ra.README).read_text())
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir() if "__" in p.name}, pinned)
        self.assertEqual(ra.verify(self.out, root=self.repo), 9)

    def test_mismatches_stop_build_and_verify(self):
        ra.build(self.store, self.out, root=self.repo)
        (self.out / "stray.bin").write_bytes(b"x")
        with self.assertRaises(SystemExit):
            ra.verify(self.out, root=self.repo)
        with self.assertRaises(SystemExit):
            ra.build(self.store, self.out, root=self.repo)
        (self.out / "stray.bin").unlink()
        (self.out / ra.README).write_text("edited\n")
        with self.assertRaises(SystemExit):
            ra.verify(self.out, root=self.repo)
        (self.store / "terminalbench/task-assets/x/y.tar.gz").write_bytes(b"other")
        with self.assertRaises(SystemExit):
            ra.build(self.store, self.out, root=self.repo)


if __name__ == "__main__":
    unittest.main()
