"""Editing Builder switch: render-time, Builder-only, generated catalog (stdlib unittest; no network, no docker).

Builds a minimal fake rendered install from the repository's control plane and task trees with the release token
mapping left as-is (the anchors do not involve tokens), switches it to two different Builder models, and checks that
(1) every planned site changes and nothing else does, (2) the lower agent and judge literals stay deepseek-flash,
(3) the catalog pin equals the sha of the generated catalog bytes, and (4) a second apply is a no-op.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_builder_model as ebm  # noqa: E402


def fake_install(dest: Path) -> Path:
    root = dest / "editing"
    shutil.copytree(ROOT / "runners/editing/control", root / "control")
    shutil.copytree(ROOT / "runners/editing/tools", root / "tools")
    for p in (ROOT / "runners/editing/state").glob("*"):
        if p.is_file():
            shutil.copy2(p, root / "control" / p.name)
    for key, task in ebm.TREE_KEYS.items():
        shutil.copytree(ROOT / "tasks/editing" / task / "tree", root / "tasks" / task / "tree", symlinks=True)
    return root


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class BuilderSwitch(unittest.TestCase):
    def test_two_models_change_only_builder_sites(self):
        for model, effort in (("deepseek-v4-pro", "max"), ("gpt-5.6-sol", "xhigh")):
            with tempfile.TemporaryDirectory() as tmp:
                root = fake_install(Path(tmp))
                before = snapshot(root)
                planned = ebm.plan(model, effort, root, ROOT)
                self.assertEqual(planned["report"]["problems"], [], (model, planned["report"]["problems"]))
                report = ebm.apply(model, effort, root, ROOT)
                after = snapshot(root)
                changed = {k for k in after if before.get(k) != after[k]}
                expected = {p.relative_to(root).as_posix() for p in planned["files"]}
                self.assertEqual(changed - {k for k in changed if k.endswith(".pyc")}, expected)
                # lower agent and judges keep deepseek-flash
                judge = (root / "control/judge_broker_xhigh.py").read_text()
                self.assertIn("deepseek-flash", judge)
                self.assertEqual(before["control/judge_broker_xhigh.py"], after["control/judge_broker_xhigh.py"])
                readiness = (root / "tools/launch_readiness.py").read_text()
                self.assertIn("'lower_model': 'deepseek-flash'", readiness)
                self.assertIn("'builder_model': '%s'" % model, readiness)
                # the catalog pin is the sha of the generated catalog
                catalog = (root / "control/codex_model_catalog_lite.json").read_bytes()
                seg = (root / "control/builder_segments.py").read_text()
                self.assertIn('LITE_MODEL_CATALOG_SHA256 = "%s"' % hashlib.sha256(catalog).hexdigest(), seg)
                self.assertIn('"slug": "%s"' % model, catalog.decode())
                # idempotent
                again = ebm.plan(model, effort, root, ROOT)
                self.assertEqual(again["files"], {})
                self.assertEqual(again["report"]["problems"], [])
                self.assertGreater(len(report["edits"]), 40)

    def test_another_endpoint_changes_only_the_builder_transport(self):
        url = "https://api.openai.com/v1"
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_install(Path(tmp))
            before = snapshot(root)
            planned = ebm.plan("deepseek-flash", "max", root, ROOT, base_url=url)
            self.assertEqual(planned["report"]["problems"], [])
            ebm.apply("deepseek-flash", "max", root, ROOT, base_url=url)
            after = snapshot(root)
            changed = {k for k in after if before.get(k) != after[k] and not k.endswith(".pyc")}
            self.assertTrue(changed)
            self.assertTrue(all(k.endswith(("direct_harbor_builder.py", "native_builder_runner.py")) for k in changed),
                            changed)
            self.assertEqual(len([k for k in changed if k.endswith("direct_harbor_builder.py")]), 9)
            control = (root / "control/direct_harbor_builder.py").read_text()
            self.assertIn("def write_provider(path, base_url=%r):" % url, control)
            self.assertIn("provider.hostname not in ('api.deepseek.com', 'api.openai.com')", control)
            self.assertIn("values.get('AGENTSWE_EDITING_BUILDER_API_KEY') or values.get('DEEPSEEK_API_KEY')", control)
            # lower agent and judges keep their DeepSeek endpoints
            self.assertEqual(before["control/judge_broker_xhigh.py"], after["control/judge_broker_xhigh.py"])
            self.assertEqual(ebm.plan("deepseek-flash", "max", root, ROOT, base_url=url)["files"], {})

    def test_default_builder_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_install(Path(tmp))
            self.assertEqual(ebm.plan("deepseek-flash", "max", root, ROOT)["files"], {})


if __name__ == "__main__":
    unittest.main()
