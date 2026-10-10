"""OSWorld evaluator-side files are pinned fixtures, served from the attempt's fixture cache (stdlib, no network).

    python3 -m unittest tests/test_osworld_eval_fixtures.py

DesktopEnv.evaluate() fetches cloud_file gold files into <cache_dir>/<dest> and postconfig downloads into
<cache_dir>/<uuid5(url)>_<basename(path)>, skipping the download when that file already exists. Every such file of
the split is pinned in fixture_manifest.json (role "eval") and in task.json's setup assets, so prepare_fixture_cache
places it where the getter looks and evaluate() needs no host download. The only evaluator URLs left unpinned are
test_006's live web pages (info_from_website).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "runners" / "optimization" / "optimization_native" / "osworld_images"
TASK = ROOT / "tasks" / "optimization" / "osworld" / "task.json"
LIVE_WEB_ONLY = {"test_006"}


def evaluator_downloads() -> tuple[list[dict], list[dict]]:
    """(pinnable files, other URLs) referenced by the evaluators of the split's cases."""
    cases = json.loads((IMAGES / "case_manifest.json").read_text())["cases"]
    meta = json.loads((IMAGES / "task_metadata.json").read_text())
    files, other = [], []

    def walk(case: str, tid: str, node, postconfig: bool) -> None:
        if isinstance(node, list):
            for item in node:
                walk(case, tid, item, postconfig)
        elif isinstance(node, dict):
            if node.get("type") == "cloud_file":
                paths = node["path"] if node.get("multi") else [node["path"]]
                dests = node["dest"] if node.get("multi") else [node["dest"]]
                files.extend({"case": case, "task_id": tid, "url": p, "cache_name": d} for p, d in zip(paths, dests))
                return
            if node.get("type") == "download" and postconfig:
                for f in node["parameters"]["files"]:
                    name = f"{uuid.uuid5(uuid.NAMESPACE_URL, f['url'])}_{os.path.basename(f['path'])}"
                    files.append({"case": case, "task_id": tid, "url": f["url"], "cache_name": name})
                return
            for value in node.values():
                walk(case, tid, value, postconfig)
        elif isinstance(node, str) and node.startswith(("http://", "https://")):
            other.append({"case": case, "task_id": tid, "url": node})

    for case, row in sorted(cases.items()):
        evaluator = meta[row["task_id"]]["task"].get("evaluator", {})
        for key, value in evaluator.items():
            walk(case, row["task_id"], value, key == "postconfig")
    return files, other


class EvalFixturesPinned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files, cls.other = evaluator_downloads()
        cls.manifest = json.loads((IMAGES / "fixture_manifest.json").read_text())["files"]
        cls.assets = {a["path"]: a for a in json.loads(TASK.read_text())["runner_config"]["assets"]["files"]}

    def test_every_evaluator_download_has_one_eval_row(self):
        eval_rows = {(r["task_id"], r["cache_name"]): r for r in self.manifest if r["role"] == "eval"}
        self.assertEqual(len(eval_rows), len([r for r in self.manifest if r["role"] == "eval"]))  # no duplicates
        wanted = {(f["task_id"], f["cache_name"]) for f in self.files}
        self.assertEqual(set(eval_rows), wanted)
        self.assertEqual(len(wanted), 25)

    def test_eval_rows_are_setup_assets_with_the_evaluator_url(self):
        urls = {(f["task_id"], f["cache_name"]): f["url"] for f in self.files}
        for row in (r for r in self.manifest if r["role"] == "eval"):
            asset = self.assets.get(row["source_path"])
            self.assertIsNotNone(asset, row["source_path"])
            self.assertEqual((asset["sha256"], asset["size"]), (row["sha256"], row["size"]))
            self.assertEqual(asset["urls"], [urls[(row["task_id"], row["cache_name"])]])
            self.assertEqual(asset["release_path"], "osworld/" + row["source_path"])
            self.assertEqual(Path(row["cache_name"]).name, row["cache_name"])

    def test_eval_names_do_not_shadow_setup_fixtures(self):
        setup = {(r["task_id"], r["cache_name"]) for r in self.manifest if r["role"] == "setup"}
        self.assertFalse(setup & {(r["task_id"], r["cache_name"]) for r in self.manifest if r["role"] == "eval"})

    def test_only_live_web_pages_remain_unpinned(self):
        self.assertEqual({o["case"] for o in self.other}, LIVE_WEB_ONLY)


class FixtureCacheServesEvalRows(unittest.TestCase):
    def test_eval_row_lands_where_the_getter_looks(self):
        sys.path.insert(0, str(IMAGES.parent))
        try:
            from osworld_fixtures import prepare_fixture_cache
        finally:
            sys.path.remove(str(IMAGES.parent))
        with tempfile.TemporaryDirectory() as tmp:
            assets, cache = Path(tmp) / "assets", Path(tmp) / "cache"
            rows = []
            for role, name in (("setup", "11111111-2222-5333-8444-555555555555_input.xlsx"), ("eval", "gold.xlsx")):
                data = f"{role} bytes".encode()
                rel = f"fixture-downloads/{'eval/t1/' if role == 'eval' else ''}{name}"
                (assets / rel).parent.mkdir(parents=True, exist_ok=True)
                (assets / rel).write_bytes(data)
                rows.append({"cache_name": name, "role": role, "sha256": hashlib.sha256(data).hexdigest(),
                             "size": len(data), "source_path": rel, "task_id": "t1"})
            manifest = Path(tmp) / "fixture_manifest.json"
            manifest.write_text(json.dumps({"schema_version": "1.0", "files": rows}))
            saved = os.environ.get("AGENTSWE_OSWORLD_ASSETS")
            os.environ["AGENTSWE_OSWORLD_ASSETS"] = str(assets)
            try:
                evidence = prepare_fixture_cache(manifest, "t1", cache)
            finally:
                if saved is None:
                    os.environ.pop("AGENTSWE_OSWORLD_ASSETS", None)
                else:
                    os.environ["AGENTSWE_OSWORLD_ASSETS"] = saved
            self.assertEqual((cache / "t1" / "gold.xlsx").read_bytes(), b"eval bytes")
            self.assertEqual(sorted(e["role"] for e in evidence), ["eval", "setup"])


if __name__ == "__main__":
    unittest.main()
