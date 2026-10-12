"""A complete Editing readiness smoke exits 0 (DeepCode claim traceability).

    python3 -m unittest tests/test_editing_smoke_exit_code.py      (standard library only; no Docker, no models)

Under a readiness binding the DeepCode one-stop skips the acceptance finalizer on purpose: the readiness judge
smoke is the single judge evidence, and a second judge call would break the one-logical-request contract. Its exit
status still required `finalizer_exit == 0`, so every complete, valid smoke exited 2 and `agentswe status` showed
the unit as failed (a smoke with status pilot_pipeline_complete, hidden
execution complete, no infrastructure classification, readiness judges complete, unit exit status 2).

A readiness smoke now succeeds when its hidden execution is complete, nothing in it is infrastructure-invalid and
its judge smoke completed. Pilot and formal runs keep the finalizer gate unchanged.
"""
from __future__ import annotations

import ast
import importlib.util
import itertools
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "runners" / "editing" / "control"
TREE = ROOT / "tasks" / "editing" / "deepcode-claim-traceability" / "tree"
ONE_STOP = TREE / "harbor" / "formal_one_stop.py"


def load_one_stop():
    """Import formal_one_stop.py from a rendered private copy of the tree, as its scripts run it."""
    scratch = Path(tempfile.mkdtemp(prefix="smoke-exit-"))
    try:
        tree = scratch / "tree"
        shutil.copytree(TREE, tree, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".runtime"))
        for path in tree.rglob("*.py"):
            if path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
            if "@@AGENTSWE_EDITING_CONTROL@@" in text:
                path.write_text(text.replace("@@AGENTSWE_EDITING_CONTROL@@", str(CONTROL)),
                                encoding="utf-8", errors="surrogateescape")
        path = tree / "harbor" / "formal_one_stop.py"
        before_path, before_modules = list(sys.path), set(sys.modules)
        sys.path[:0] = [str(path.parent), str(tree), str(CONTROL)]
        try:
            spec = importlib.util.spec_from_file_location("smoke_exit_deepcode_one_stop", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = before_path
            for key in set(sys.modules) - before_modules - {spec.name}:
                sys.modules.pop(key, None)
        return module
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


ONE = load_one_stop()
JUDGES_COMPLETE = {"profile": "single-dev-two-round-hidden-smoke-v1", "evaluation_mode": "readiness_smoke",
                   "readiness_judges_complete": True, "exits": {"result": 0}}


def code(**override):
    """Exit code for a complete smoke's end state, with fields overridden."""
    fields = {"readiness": True, "execution_complete": True, "infrastructure_invalid": [],
              "readiness_judges": JUDGES_COMPLETE, "finalizer_exit": None, "pilot": True,
              "acceptance_complete": False}
    fields.update(override)
    return ONE.one_stop_exit_code(**fields)


class ReadinessSmokeExit(unittest.TestCase):
    def test_complete_smoke_without_finalizer_exits_zero(self):
        # A complete smoke's end state: finalizer skipped (None), acceptance never computed.
        self.assertEqual(code(), 0)

    def test_infrastructure_invalid_smoke_exits_two(self):
        self.assertEqual(code(infrastructure_invalid=["provider_infrastructure_failure"]), 2)

    def test_incomplete_hidden_execution_exits_two(self):
        self.assertEqual(code(execution_complete=False), 2)

    def test_incomplete_or_missing_judge_smoke_exits_two(self):
        self.assertEqual(code(readiness_judges=dict(JUDGES_COMPLETE, readiness_judges_complete=False)), 2)
        self.assertEqual(code(readiness_judges=None), 2)


class FormalAndPilotExitUnchanged(unittest.TestCase):
    def test_matches_the_previous_expression_without_readiness(self):
        # Previous: 0 if execution_complete and not infrastructure_invalid and finalizer_exit == 0
        #               and (not pilot or acceptance_complete) else 2
        for complete, invalid, finalizer, pilot, acceptance, judges in itertools.product(
                (True, False), ([], ["broker_infrastructure_failure"]), (None, 0, 1, 2), (True, False),
                (True, False), (None, JUDGES_COMPLETE)):
            previous = 0 if complete and not invalid and finalizer == 0 and (not pilot or acceptance) else 2
            with self.subTest(complete=complete, invalid=invalid, finalizer=finalizer, pilot=pilot,
                              acceptance=acceptance, judges=judges is not None):
                self.assertEqual(code(readiness=False, execution_complete=complete, infrastructure_invalid=invalid,
                                      finalizer_exit=finalizer, pilot=pilot, acceptance_complete=acceptance,
                                      readiness_judges=judges), previous)


class RunExecutionUsesTheHelper(unittest.TestCase):
    def test_final_return_is_the_readiness_aware_exit(self):
        tree = ast.parse(ONE_STOP.read_text(encoding="utf-8"))
        run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_execution")
        handled = next(node for node in ast.walk(run) if isinstance(node, ast.Try))
        final = handled.body[-1]
        self.assertIsInstance(final, ast.Return)
        self.assertIsInstance(final.value, ast.Call)
        self.assertEqual(getattr(final.value.func, "id", None), "one_stop_exit_code")
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in final.value.keywords}
        self.assertEqual(keywords.get("readiness"), "readiness_binding is not None")
        self.assertEqual(keywords.get("finalizer_exit"), "finalizer_exit")
        self.assertEqual(keywords.get("readiness_judges"), "readiness_judges")


if __name__ == "__main__":
    unittest.main()
