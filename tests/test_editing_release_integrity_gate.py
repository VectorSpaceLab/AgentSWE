"""The formal readiness gate's release mode (stdlib unittest; a temporary control directory, no evidence homes).

DeepCode, DeepTutor, Dyad and OpenWiki call control/readiness_admission.require_formal_readiness from their own
formal_one_stop.py under --run-formal, so a release formal run needs the gate's release mode inside the unit as well
as in the launcher: with AGENTSWE_EDITING_FORMAL_GATE=release-integrity exactly, only the source checks run; unset or
any other value keeps the full gate (a READY gate row and a valid admission)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "runners" / "editing" / "control"
sys.path.insert(0, str(CONTROL))

import audit_readiness as audit  # noqa: E402
import readiness_admission as admission  # noqa: E402

TREES_WITH_IN_TREE_GATE = ("deepcode-claim-traceability", "deeptutor-adaptive-remediation",
                           "dyad-acceptance-driven", "openwiki-change-impact")


class ReleaseIntegrityGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.control = base / "control"
        self.control.mkdir()
        self.template = base / "template"
        (self.template / "meta").mkdir(parents=True)
        (self.template / "meta" / "0905_case_contract.json").write_text('{"task": "t"}')
        self.sibling = base / "rendered"
        (self.sibling / "meta").mkdir(parents=True)
        (self.sibling / "meta" / "0905_case_contract.json").write_text(
            json.dumps({"task": "t", "sibling": str(self.sibling)}))
        (self.sibling / "evaluator.py").write_text("print('evaluate')\n")
        (self.control / "formal_config.py").write_text(
            f"from pathlib import Path\nTASKS = {{'t': Path({str(self.sibling)!r})}}\n"
            f"SMOKE_ROOT = Path({str(base / 'smokes')!r})\n")
        (self.control / "post_repair_tree_snapshot.json").write_text(
            json.dumps({"tasks": {"t": {"sibling": {"digest": audit.tree_digest(self.sibling)}}}}))
        (self.control / "pre_repair_tree_snapshot.json").write_text(json.dumps(
            {"tasks": {"t": {"source": {"path": str(self.template), "digest": audit.tree_digest(self.template)}}}}))
        # a fresh install's gate: the audit ran, nothing is admitted
        (self.control / "formal_readiness_gate.json").write_text(json.dumps(
            {"profile": admission.PROFILE, "tasks": [{"task": "t", "readiness": "REPAIR", "errors": ["no admission"]}]}))
        patcher = mock.patch.object(admission, "ROOT", self.control)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gate(self, mode):
        env = {k: v for k, v in os.environ.items() if k != "AGENTSWE_EDITING_FORMAL_GATE"}
        if mode is not None:
            env["AGENTSWE_EDITING_FORMAL_GATE"] = mode
        with mock.patch.dict(os.environ, env, clear=True):
            return admission.require_formal_readiness(self.sibling)

    def test_release_mode_passes_an_intact_tree(self):
        result = self.gate("release-integrity")
        self.assertEqual(result["mode"], "release-integrity")
        self.assertEqual(result["task"], "t")
        self.assertEqual(result["sibling_digest"], audit.tree_digest(self.sibling))
        self.assertEqual(result["readiness"], "not required (release integrity check)")
        self.assertIsNone(result["admission"])
        self.assertIsNone(result["gate"])
        self.assertTrue({"task", "sibling_digest", "readiness", "admission", "gate"} <= set(result))

    def test_release_mode_refuses_a_tampered_tree(self):
        (self.sibling / "evaluator.py").write_text("print('evaluate')\nx = 1\n")
        with self.assertRaisesRegex(ValueError, "differs from the snapshot setup recorded"):
            self.gate("release-integrity")

    def test_release_mode_refuses_a_changed_template(self):
        (self.template / "added.md").write_text("changed after setup\n")
        with self.assertRaisesRegex(ValueError, "release template tree changed"):
            self.gate("release-integrity")

    def test_release_mode_keeps_the_identity_check(self):
        (self.sibling / "meta" / "0905_case_contract.json").write_text(json.dumps({"task": "t", "sibling": "/elsewhere"}))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.gate("release-integrity")

    def test_default_mode_still_requires_a_ready_row(self):
        for mode in (None, "", "1", "true", "release", "RELEASE-INTEGRITY"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "requires a current READY readiness gate row"):
                    self.gate(mode)

    def test_default_mode_with_a_ready_row_still_requires_an_admission(self):
        (self.control / "formal_readiness_gate.json").write_text(json.dumps(
            {"profile": admission.PROFILE, "tasks": [{"task": "t", "readiness": "READY", "errors": []}]}))
        with self.assertRaisesRegex(ValueError, "admission missing or invalid"):
            self.gate(None)


class Wiring(unittest.TestCase):
    def test_trees_that_gate_in_tree_call_the_control_gate(self):
        for task in TREES_WITH_IN_TREE_GATE:
            source = (ROOT / "tasks" / "editing" / task / "tree" / "harbor" / "formal_one_stop.py").read_text()
            self.assertIn("@@AGENTSWE_EDITING_CONTROL@@/readiness_admission.py", source, task)
            self.assertIn("require_formal_readiness(ROOT)", source, task)

    def test_launcher_sets_release_mode_on_the_unit_only_with_integrity_only(self):
        stubs = {name: types.ModuleType(name) for name in ("formal_config", "formal_commands", "control_runtime")}
        stubs["formal_config"].TASKS = {}
        stubs["control_runtime"].control_command = lambda python, script, *a: [str(python), str(script), *a]
        spec = importlib.util.spec_from_file_location("launch_formal_task_gate",
                                                      ROOT / "runners" / "editing" / "tools" / "launch_formal_task.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        self.assertEqual(module.FORMAL_GATE_RELEASE, admission.RELEASE_INTEGRITY)
        source = Path(spec.origin).read_text()
        self.assertIn("    if a.integrity_only:\n        env['AGENTSWE_EDITING_FORMAL_GATE'] = FORMAL_GATE_RELEASE\n", source)
        self.assertIn("os.environ.pop('AGENTSWE_EDITING_FORMAL_GATE', None)", source)


if __name__ == "__main__":
    unittest.main()
