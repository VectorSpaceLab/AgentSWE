"""`agentswe freeze`: the budget freeze of a formal Editing run cut at its time budget (stdlib unittest, no Docker).

agentswe/runners/editing_budget_freeze.py is the CLI side (task and mode refusals, owner check, the credential file
`run` writes, the tool invocation); runners/editing/tools/budget_freeze.py is the tool (the trigger, the ledger and
backups, and each tree's freeze, hidden and finalize stages through the tree's own code). Every model, broker and
container here is a fake: the tests check what the stages call and with which arguments and environment.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402
from agentswe.runners import editing_budget_freeze as cli_freeze  # noqa: E402

TOOL = ROOT / "runners" / "editing" / "tools" / "budget_freeze.py"
spec = importlib.util.spec_from_file_location("budget_freeze_under_test", TOOL)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

TASK_IDS = dict(ed.TASK_KEYS)
LABEL = "oss-formal-s1-20260101t000000z"
NO_TERMINAL = bf.NO_TERMINAL


def write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))
    return path


AUDIT = '''import hashlib, os
from pathlib import Path
def tree_digest(root):
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = Path(base) / name
            h.update(str(p.relative_to(root)).encode()); h.update(p.read_bytes())
    return h.hexdigest()
'''


class Home:
    """A synthetic install: control/ (formal_config, audit_readiness, result_judge), ten trees, formal runs."""

    def __init__(self, base: Path):
        self.base = base
        self.control = base / "editing" / "control"
        self.formal = base / "runs" / "editing" / "formal"
        tasks = {key: base / "editing" / "tasks" / tid / "tree" for tid, key in TASK_IDS.items()}
        for tree in tasks.values():
            write(tree / "harbor" / "formal_one_stop.py", "# one-stop\n")
        write(self.control / "formal_config.py", "from pathlib import Path\n"
              f"ROOT = Path({str(self.control)!r})\nFORMAL_ROOT = Path({str(self.formal)!r})\n"
              f"TASKS = {{{', '.join(f'{k!r}: Path({str(v)!r})' for k, v in tasks.items())}}}\n"
              "RESULT_JUDGE = ROOT / 'result_judge.py'\nCREATE_CODE_JUDGE = ROOT / 'code_judge_runner.py'\n"
              "ALIGNMENT_SNAPSHOT = ROOT / 'create_alignment_snapshot.json'\n"
              f"CONTROL_PYTHON = Path({sys.executable!r})\n")
        write(self.control / "audit_readiness.py", AUDIT)
        write(self.control / "result_judge.py", "# judge\n")
        self.tasks = tasks
        sys.path.insert(0, str(self.control))
        self.audit = bf.load_module("budget_freeze_test_audit", self.control / "audit_readiness.py")

    def run(self, task: str = "openwiki", *, label: str = LABEL, unit_env: dict | None = None) -> Path:
        name = f"0905-edit-codex-xhigh-{label}-{task}"
        run = self.formal / "codex_xhigh" / task / name
        run.mkdir(parents=True)
        tree = self.tasks[task]
        command = [sys.executable, "-E", "-s", "-B", str(tree / "harbor" / "formal_one_stop.py"), "--run-formal",
                   "--run-dir", str(run), "--credential-file", str(self.base / "secrets" / "editing" / "credential.env"),
                   "--max-dev-rounds", "5", "--n-concurrent", "1", "--builder-timeout", "18000"]
        record = {"task": task, "label": label, "run_id": name, "run_dir": str(run),
                  "unit": f"agentswe-oss-formal-{task}-{label}", "command": command,
                  "gate": {"mode": "release-integrity"}, "sibling_digest": self.audit.tree_digest(tree),
                  "result_judge_sha256": hashlib.sha256((self.control / "result_judge.py").read_bytes()).hexdigest()}
        if unit_env is not None:
            record["unit_environment"] = unit_env
        write(self.formal / "launch_control" / f"0905-edit-codex-xhigh-{label}" / task / "launch_record.json", record)
        return run


def budget_cut(run: Path, *, exit_reason="infrastructure_cut", exception="AgentTimeoutError", resume=False,
               before_deadline=70.0) -> None:
    write(run / "builder_segment_receipt.json", {
        "builder_deadline_epoch": 1000.0 + before_deadline,
        "attempts": [{"segment_index": 1, "exit_reason": exit_reason, "exit_code": 0, "ended_at_epoch": 1000.0,
                      "harbor_exception": {"exception_type": exception} if exception else None,
                      "decision": {"resume": resume, "refusals": ["no budget left"], "remaining_seconds": 0}}]})


class FakeAdapter(bf.Adapter):
    """Reads a simple fixture: fixture.json {records, strict, frozen}; sources under src/<round>."""
    gate_status = ("builder_lifecycle_incomplete",)

    def _fx(self):
        return json.loads((self.run.run / "fixture.json").read_text())

    def records(self):
        return self._fx()["records"]

    def frozen_on_disk(self):
        return self._fx().get("frozen")

    def latest_source(self, record):
        return self.run.run / "src" / str(record["round"]), record["candidate_digest"]

    def digest(self, path):
        return hashlib.sha256((Path(path) / "f").read_bytes()).hexdigest()

    def strict_native(self):
        return self._fx()["strict"]

    def freeze_paths(self):
        return [self.run.lifecycle / "freeze_manifest.json"]

    def hidden_paths(self):
        return [self.run.lifecycle / "hidden-result.json"]

    def hidden_attestation(self):
        return self.run.lifecycle / "hidden-after-freeze-attestation.json"

    def freeze_rewrites(self):
        return [self.run.run / "summary.json"]

    def plan_hidden(self):
        return {"runner": "fake"}

    def finalize_argv(self, endpoint):
        return ["finalize", endpoint]


def fixture(run: Path, n: int = 2, *, strict_errors=(NO_TERMINAL,), status="builder_lifecycle_incomplete") -> None:
    records = []
    for i in range(1, n + 1):
        write(run / "src" / str(i) / "f", f"candidate {i}")
        records.append({"round": i, "candidate_digest": hashlib.sha256(f"candidate {i}".encode()).hexdigest()})
    write(run / "fixture.json", {"records": records, "strict": {"valid": False, "errors": list(strict_errors)}})
    write(run / "summary.json", {"status": status})


class ToolBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Home(Path(tmp.name))
        self.addCleanup(lambda: sys.path.remove(str(self.home.control)) if str(self.home.control) in sys.path else None)
        for name, value in (("unit_state", "failed"), ("unit_recorded_environment", None)):
            patcher = mock.patch.object(bf, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(bf, "containers_mounting", return_value=([], None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ready(self, task="openwiki", n=2, **kw):
        run_dir = self.home.run(task)
        budget_cut(run_dir, **{k: v for k, v in kw.items() if k in ("exit_reason", "exception", "resume",
                                                                      "before_deadline")})
        fixture(run_dir, n, **{k: v for k, v in kw.items() if k in ("strict_errors", "status")})
        run = bf.Run(run_dir, control=self.home.control)
        ad = FakeAdapter(run, types.SimpleNamespace(), argparse.Namespace(max_dev_rounds=5))
        return run, ad

    def gates(self, run, ad, stage="freeze", apply=False):
        g, facts = bf.trigger_gates(run, ad, stage, bf.Ledger(run), apply)
        return g, facts

    def failed(self, run, ad, stage="freeze", apply=False):
        g, _ = self.gates(run, ad, stage, apply)
        return [name for name, ok in g.items() if not ok]


class RunResolution(ToolBase):
    def test_supported_run_resolves_from_its_own_records(self):
        run_dir = self.home.run("openwiki")
        run = bf.Run(run_dir, control=self.home.control)
        self.assertEqual((run.task, run.label), ("openwiki", LABEL))
        self.assertEqual(run.unit_service, f"agentswe-oss-formal-openwiki-{LABEL}.service")
        self.assertEqual(run.credential, self.home.base / "secrets" / "editing" / "credential.env")
        self.assertEqual(run.argv[0], "--run-formal")
        self.assertEqual(run.applications_log, self.home.formal / "budget_freeze_applications.jsonl")

    def test_other_editing_tasks_are_not_supported_yet(self):
        for key in sorted(set(TASK_IDS.values()) - set(bf.SUPPORTED)):
            run_dir = self.home.run(key)
            with self.assertRaises(bf.NotSupported) as caught:
                bf.Run(run_dir, control=self.home.control)
            self.assertIn("does not support", str(caught.exception))
            self.assertIn(key, str(caught.exception))

    def test_paths_outside_the_formal_root_or_unnamed_runs_are_refused(self):
        stray = self.home.base / "elsewhere" / "openwiki" / f"0905-edit-codex-xhigh-{LABEL}-openwiki"
        stray.mkdir(parents=True)
        with self.assertRaisesRegex(bf.Refusal, "formal run root"):
            bf.Run(stray, control=self.home.control)
        odd = self.home.formal / "codex_xhigh" / "openwiki" / "something-else"
        odd.mkdir(parents=True)
        with self.assertRaisesRegex(bf.Refusal, "not a formal run directory name"):
            bf.Run(odd, control=self.home.control)

    def test_launch_record_must_exist_and_describe_the_run(self):
        run_dir = self.home.run("deeptutor")
        record = self.home.formal / "launch_control" / f"0905-edit-codex-xhigh-{LABEL}" / "deeptutor" / "launch_record.json"
        value = json.loads(record.read_text())
        for key, bad in (("run_dir", "/elsewhere"), ("unit", "agentswe-formal-other"), ("command", ["python3", "x.py"])):
            write(record, {**value, key: bad})
            with self.assertRaisesRegex(bf.Refusal, "does not describe this run"):
                bf.Run(run_dir, control=self.home.control)
        record.unlink()
        with self.assertRaisesRegex(bf.Refusal, "launch record missing"):
            bf.Run(run_dir, control=self.home.control)

    def test_unit_names_without_the_oss_prefix_are_accepted(self):
        run_dir = self.home.run("aider")
        record = self.home.formal / "launch_control" / f"0905-edit-codex-xhigh-{LABEL}" / "aider" / "launch_record.json"
        write(record, {**json.loads(record.read_text()), "unit": f"agentswe-formal-aider-{LABEL}"})
        self.assertEqual(bf.Run(run_dir, control=self.home.control).unit, f"agentswe-formal-aider-{LABEL}")


class Environment(ToolBase):
    UNIT = {"PYTHONUNBUFFERED": "1", "AGENTSWE_EDIT_EARLY_STOP_RESAMPLE": "1", "AGENTSWE_RESULT_JUDGE": "/c/j.py"}

    def test_recorded_unit_environment_is_used(self):
        run = bf.Run(self.home.run("openwiki", unit_env=self.UNIT), control=self.home.control)
        env, source = run.unit_environment()
        self.assertEqual((env, source), (self.UNIT, "launch_record"))

    def test_older_launch_records_rebuild_the_launcher_table(self):
        run = bf.Run(self.home.run("aider"), control=self.home.control)
        env, source = run.unit_environment()
        self.assertIn("rebuilt", source)
        self.assertEqual(env["AGENTSWE_EDITING_FORMAL_GATE"], "release-integrity")  # gate mode release-integrity
        self.assertEqual(env["AGENTSWE_EDIT_EARLY_STOP_RESAMPLE"], "1")
        self.assertEqual(env["AGENTSWE_RESULT_JUDGE"], str(self.home.control / "result_judge.py"))

    def test_systemd_record_of_the_unit_must_agree(self):
        run = bf.Run(self.home.run("openwiki", unit_env=self.UNIT), control=self.home.control)
        with mock.patch.object(bf, "unit_recorded_environment", return_value={**self.UNIT, "PYTHONUNBUFFERED": "0"}):
            with self.assertRaisesRegex(bf.Refusal, "PYTHONUNBUFFERED"):
                run.environment()
        with mock.patch.object(bf, "unit_recorded_environment", return_value=dict(self.UNIT)), \
                mock.patch.object(bf, "out", return_value=(0, "PATH=/usr/bin:/bin\nLANG=C.UTF-8\nPYTHONPATH=/x\n")):
            env, info = run.environment()
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["AGENTSWE_RESULT_JUDGE"], "/c/j.py")
        self.assertIn("equal to the Environment systemd recorded", info["systemd_check"])
        self.assertEqual(info["variables"], self.UNIT)


class OneStopArguments(unittest.TestCase):
    def test_parser_namespace_is_captured_before_any_side_effect(self):
        effects = []

        def main(argv=None):
            p = argparse.ArgumentParser()
            p.add_argument("--run-dir")
            p.add_argument("--upstream", default=os.environ.get("BF_TEST_UPSTREAM", "https://default"))
            p.add_argument("--max-dev-rounds", type=int, default=10)
            args = p.parse_args(argv)
            effects.append(args)
            return 0

        with mock.patch.dict(os.environ, {"BF_TEST_UPSTREAM": "https://unit-env"}):
            ns = bf.formal_args(types.SimpleNamespace(main=main), ["--run-dir", "/r", "--max-dev-rounds", "5"])
        self.assertEqual((ns.run_dir, ns.max_dev_rounds, ns.upstream), ("/r", 5, "https://unit-env"))
        self.assertEqual(effects, [])
        self.assertEqual(argparse.ArgumentParser.parse_args.__qualname__, "ArgumentParser.parse_args")

    def test_mains_without_an_argv_parameter_parse_the_record_argv(self):
        def main():
            p = argparse.ArgumentParser()
            p.add_argument("--lower-image", default="image:default")
            p.add_argument("--run-formal", action="store_true")
            p.parse_args()
            raise AssertionError("main continued after parsing")

        ns = bf.formal_args(types.SimpleNamespace(main=main), ["--run-formal"])
        self.assertTrue(ns.run_formal)
        self.assertEqual(ns.lower_image, "image:default")


class Trigger(ToolBase):
    def test_budget_cut_run_passes_every_freeze_gate(self):
        run, ad = self.ready()
        self.assertEqual(self.failed(run, ad), [])

    def test_unit_still_running_refuses(self):
        run, ad = self.ready()
        with mock.patch.object(bf, "unit_state", return_value="active"):
            self.assertIn("formal unit not running", self.failed(run, ad))

    def test_tree_or_judge_changed_since_launch_refuses(self):
        run, ad = self.ready()
        write(self.home.tasks["openwiki"] / "harbor" / "extra.py", "# changed\n")
        self.assertIn("task tree digest == launch record sibling_digest", self.failed(run, ad))
        write(self.home.control / "result_judge.py", "# other judge\n")
        self.assertIn("control/result_judge.py sha256 == launch record", self.failed(run, ad))

    def cut_gate(self, run, ad):
        return [name for name in self.failed(run, ad) if name.startswith("Builder cut at the budget")]

    def test_segment_must_be_an_unresumed_budget_cut(self):
        for kw in ({"exit_reason": "completed"}, {"resume": True},
                   {"exception": "RuntimeError", "before_deadline": 3600.0},
                   {"exception": None, "before_deadline": 3600.0}):
            with self.subTest(**kw):
                run, ad = self.ready(**kw)
                self.assertTrue(self.cut_gate(run, ad), kw)
                for path in sorted(self.home.formal.rglob("*"), reverse=True):
                    path.unlink() if path.is_file() else path.rmdir()

    def test_a_cut_close_to_the_deadline_without_agent_timeout_passes(self):
        run, ad = self.ready(exception=None, before_deadline=420.0)
        self.assertEqual(self.cut_gate(run, ad), [])

    def test_strict_native_errors_must_be_exactly_the_missing_terminal_event(self):
        run, ad = self.ready(strict_errors=(NO_TERMINAL, "feedback response has no successful socket-write receipt"))
        self.assertIn("stored strict native proof fails only on the missing terminal event", self.failed(run, ad))

    def test_status_must_be_the_trees_gate_status(self):
        run, ad = self.ready(status="completed")
        self.assertTrue(any(name.startswith("summary status is") for name in self.failed(run, ad)))

    def test_accepted_count_and_digests(self):
        run, ad = self.ready(n=6)
        self.assertIn("1..5 accepted submissions", self.failed(run, ad))
        run2, ad2 = self.ready_task("deeptutor", n=2)
        fx = json.loads((run2.run / "fixture.json").read_text())
        fx["records"][1]["candidate_digest"] = fx["records"][0]["candidate_digest"]
        write(run2.run / "fixture.json", fx)
        self.assertIn("accepted submission digests distinct", self.failed(run2, ad2))

    def ready_task(self, task, n):
        run_dir = self.home.run(task)
        budget_cut(run_dir)
        fixture(run_dir, n)
        run = bf.Run(run_dir, control=self.home.control)
        return run, FakeAdapter(run, types.SimpleNamespace(), argparse.Namespace(max_dev_rounds=5))

    def test_latest_source_changed_after_acceptance_refuses(self):
        run, ad = self.ready()
        write(run.run / "src" / "2" / "f", "edited after acceptance")
        self.assertIn("latest accepted source present and its digest unchanged since acceptance", self.failed(run, ad))

    def test_zero_accepted_is_refused_with_the_protocol_zero(self):
        run, ad = self.ready(n=0)
        with self.assertRaises(bf.Refusal) as caught:
            bf.stage_run(run, ad, bf.Ledger(run), "freeze", True, "tester", {}, [])
        self.assertIn("nothing to freeze", str(caught.exception))
        self.assertIn("Result is 0", str(caught.exception))
        log = []
        self.assertEqual(bf.stage_check(run, ad, bf.Ledger(run), {"source": "s", "systemd_check": "c"}, log), 1)
        self.assertTrue(any("nothing to freeze" in line for line in log))
        self.assertFalse(run.evidence.exists())

    def test_existing_evidence_refuses(self):
        run, ad = self.ready()
        write(run.lifecycle / "hidden-result.json", {})
        self.assertIn("no held-out evidence yet", self.failed(run, ad))
        write(run.run / "formal_aggregation.json", {})
        self.assertIn("no formal_aggregation.json yet", self.failed(run, ad))

    def test_a_container_mounting_the_run_refuses(self):
        run, ad = self.ready()
        with mock.patch.object(bf, "containers_mounting", return_value=(["abc123 /x running"], None)):
            self.assertIn("no container mounts this run", self.failed(run, ad))
        with mock.patch.object(bf, "containers_mounting", return_value=(None, "docker ps failed")):
            self.assertIn("no container mounts this run", self.failed(run, ad))

    def test_another_tools_manual_freeze_directory_or_backup_refuses(self):
        run, ad = self.ready()
        write(run.evidence / "manual_freeze_record.json", {"reason": "something else"})
        failed = self.failed(run, ad)
        self.assertIn("no budget freeze record yet", failed)
        with self.assertRaisesRegex(bf.Refusal, "not a record of this budget freeze"):
            with mock.patch.object(bf, "load_one_stop", return_value=types.SimpleNamespace()), \
                    mock.patch.object(bf, "formal_args", return_value=argparse.Namespace()):
                bf.execute(run, "check", False, "t", {}, [], adapter_factory=FakeAdapter)
        run2, ad2 = self.ready_task("aider", 1)
        write(run2.run / ("summary.json" + bf.SUFFIX), {})
        problems = bf.preview_backups(ad2.freeze_rewrites(), bf.Ledger(run2), [])
        self.assertTrue(problems and "backup already exists" in problems[0])

    def test_owner_mismatch_refuses(self):
        run, ad = self.ready()
        with mock.patch.object(bf.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertIn("run directory owned by the invoking user", self.failed(run, ad))

    def test_credential_gate_only_for_an_apply_of_hidden_or_finalize(self):
        run, ad = self.ready()
        name = "credential file present (written by agentswe freeze)"
        g, facts = self.gates(run, ad, "hidden", apply=False)
        self.assertNotIn(name, g)
        self.assertIn("credential", facts)
        g, _ = self.gates(run, ad, "hidden", apply=True)
        self.assertIs(g[name], False)

    def test_check_is_read_only_and_reports_the_next_stage(self):
        run, ad = self.ready()
        before = bf.snapshot(run.run)
        log = []
        code = bf.stage_check(run, ad, bf.Ledger(run), {"source": "launch_record", "systemd_check": "c"}, log)
        self.assertEqual(code, 0, log)
        self.assertEqual(bf.snapshot(run.run), before)
        self.assertIn("next stage: freeze", log)
        self.assertIn("run directory untouched by this check: True", log)


class LedgerBackups(ToolBase):
    def test_backups_are_made_once_and_foreign_ones_refuse(self):
        run, _ = self.ready()
        ledger = bf.Ledger(run)
        ledger.value = {"schema_version": bf.RECORD_SCHEMA, "reason": bf.REASON, "backups": []}
        log = []
        summary = run.run / "summary.json"
        original = summary.read_bytes()
        ledger.backup(summary, log)
        self.assertEqual(Path(str(summary) + bf.SUFFIX).read_bytes(), original)
        write(summary, {"status": "rewritten"})
        ledger.backup(summary, log)  # this freeze's own backup: kept, not refused, not replaced
        self.assertEqual(Path(str(summary) + bf.SUFFIX).read_bytes(), original)
        ledger.backup(run.run / "new.json", log)
        self.assertIn(str(run.run / "new.json"), ledger.value["created"])
        other = run.run / "builder_session_attestation.json"
        write(other, {})
        write(Path(str(other) + bf.SUFFIX), {"made": "elsewhere"})
        with self.assertRaisesRegex(bf.Refusal, "backup already exists"):
            ledger.backup(other, log)
        record = json.loads(run.record_path.read_text())
        self.assertEqual([b["path"] for b in record["backups"]], [str(summary)])

    def test_the_freeze_stage_writes_the_record_with_reason_operator_and_stages(self):
        run, ad = self.ready()
        ad.do_freeze = lambda ledger, log: (ad.freeze_summary_note(ledger, log) or
                                            {"frozen": {"candidate_digest": "d" * 64, "source_submission": 2},
                                             "freeze_manifest_sha256": "e" * 64})
        log = []
        self.assertEqual(bf.stage_run(run, ad, bf.Ledger(run), "freeze", True, "operator-x", {"source": "s"}, log), 0)
        record = json.loads(run.record_path.read_text())
        self.assertEqual((record["reason"], record["manual"], record["operator"]),
                         ("budget_exhausted_freeze_latest_accepted", True, "operator-x"))
        self.assertEqual(record["stages"]["freeze"]["state"], "done")
        self.assertEqual(record["stages"]["freeze"]["result"]["frozen_candidate_digest"], "d" * 64)
        self.assertTrue(record["created_at"] and record["stages"]["freeze"]["finished_at"])
        summary = json.loads((run.run / "summary.json").read_text())
        self.assertEqual(summary["status"], "builder_lifecycle_incomplete")
        self.assertEqual(summary["manual_freeze"]["reason"], bf.REASON)
        self.assertTrue(Path(str(run.run / "summary.json") + bf.SUFFIX).is_file())
        # a second freeze is refused by the record; hidden is next
        self.assertIn("no budget freeze record yet", self.failed(run, ad))
        self.assertEqual(bf.next_stage(ad, bf.Ledger(run), run), "hidden")

    def test_a_failed_stage_is_recorded(self):
        run, ad = self.ready()

        def broken(ledger, log):
            raise RuntimeError("controller refused")
        ad.do_freeze = broken
        with self.assertRaises(RuntimeError):
            bf.stage_run(run, ad, bf.Ledger(run), "freeze", True, "op", {}, [])
        self.assertEqual(json.loads(run.record_path.read_text())["stages"]["freeze"]["state"], "failed")

    def test_dry_run_writes_nothing(self):
        run, ad = self.ready()
        before = bf.snapshot(run.run)
        log = []
        self.assertEqual(bf.stage_run(run, ad, bf.Ledger(run), "freeze", False, "op", {}, log), 0)
        self.assertIn("DRY RUN: nothing written or started", log)
        self.assertEqual(bf.snapshot(run.run), before)


# --------------------------------------------------------------------------------------- per-tree stage plumbing --
class Judge:
    def __init__(self, base: Path):
        self.endpoint = "http://127.0.0.1:65000/v1/responses"
        self.stats_path, self.lifecycle_path = base / "stats.json", base / "lifecycle.json"
        self.closed = False

    def close(self):
        self.closed = True
        return {"absent_after_cleanup": True}


FINALIZER = '''import json, os, sys
a = sys.argv[1:]
run = a[a.index("--run-dir") + 1]
out = a[a.index("--output") + 1] if "--output" in a else os.path.join(run, "formal_aggregation.json")
json.dump({"formal_result_publishable": True, "result_axis": {"score": 12.5}, "argv": a,
           "resample": os.environ.get("AGENTSWE_EDIT_EARLY_STOP_RESAMPLE")}, open(out, "w"))
print("finalized")
'''


class TreeStages(ToolBase):
    def adapter(self, cls, task, **args):
        run_dir = self.home.run(task)
        budget_cut(run_dir)
        write(self.home.tasks[task] / "evaluator" / "formal_finalize.py", FINALIZER)
        run = bf.Run(run_dir, control=self.home.control)
        ns = argparse.Namespace(max_dev_rounds=5, n_concurrent=1, upstream="https://upstream.example",
                                builder_image="builder:img", broker_python=sys.executable,
                                runtime_python=self.home.base / "envs" / "py", lower_image="lower:img",
                                dependency_overlay=str(self.home.base / "envs" / "overlay"), **args)
        ledger = bf.Ledger(run)
        ledger.value = {"schema_version": bf.RECORD_SCHEMA, "reason": bf.REASON, "operator": "op",
                        "created_at": "t0", "tool_sha256": "s", "backups": [], "stages": {}}
        run.evidence.mkdir()
        ledger.save()
        return run, ns, ledger

    def test_finalize_argv_per_tree(self):
        for cls, task, head, output in ((bf.OpenWiki, "openwiki", sys.executable, True),
                                        (bf.DeepTutor, "deeptutor", sys.executable, False),
                                        (bf.Aider, "aider", "python3", False)):
            run, ns, _ = self.adapter(cls, task)
            argv = cls(run, types.SimpleNamespace(), ns).finalize_argv("E")
            self.assertEqual(argv[0], head)
            self.assertEqual(argv[1], str(run.tree / "evaluator" / "formal_finalize.py"))
            self.assertEqual(argv[argv.index("--run-dir") + 1], str(run.run))
            self.assertEqual(argv[argv.index("--credential-file") + 1], str(run.credential.resolve()))
            self.assertEqual(argv[-2:], ["--result-judge-broker-endpoint", "E"])
            self.assertEqual("--output" in argv, output, task)

    def test_openwiki_finalize_uses_a_fresh_judge_and_writes_the_summaries(self):
        run, ns, ledger = self.adapter(bf.OpenWiki, "openwiki")
        write(run.run / "summary.json", {"status": "builder_lifecycle_incomplete"})
        write(run.lifecycle / "hidden-result.json", {"formal_result_eligible": True})
        started = []
        fos = types.SimpleNamespace(RESULT_JUDGE_MODEL="deepseek-flash", RESULT_JUDGE_EFFORT="max",
                                    result_judge_stats=lambda endpoint: {"runtime": {"calls": 3}})
        judge = Judge(run.evidence)

        def start_result_judge(directory, credential, upstream, image):
            started.append((directory, credential, upstream, image))
            return judge
        fos.start_result_judge = start_result_judge
        ad = bf.OpenWiki(run, fos, ns)
        written = []
        ad.shared_summary_writer = lambda: (lambda r, **kw: (written.append(kw), write(r / "one_stop_summary.json",
                                                                                         {"schema": "v2"})))
        with mock.patch.dict(os.environ, {"AGENTSWE_EDIT_EARLY_STOP_RESAMPLE": "1"}):
            result = ad.do_finalize(ledger, [])
        self.assertEqual(started, [(run.evidence, run.credential.resolve(), "https://upstream.example", "builder:img")])
        self.assertTrue(judge.closed)
        self.assertEqual(result["finalizer_exit"], 0)
        agg = json.loads((run.run / "formal_aggregation.json").read_text())
        self.assertEqual(agg["resample"], "1")  # the finalizer runs with the unit's environment
        self.assertEqual(agg["argv"][agg["argv"].index("--result-judge-broker-endpoint") + 1], judge.endpoint)
        summary = json.loads((run.run / "summary.json").read_text())
        self.assertEqual((summary["status"], summary["max_dev_rounds"], summary["n_concurrent"]), ("completed", 5, 1))
        self.assertEqual(summary["manual_freeze"]["reason"], bf.REASON)
        self.assertEqual(summary["result_judge_broker"]["model"], "deepseek-flash")
        self.assertEqual(written, [{"max_dev_rounds": 5, "n_concurrent": 1, "mode": "formal"}])
        self.assertEqual(json.loads((run.run / "one_stop_summary.json").read_text())["manual_freeze"]["reason"], bf.REASON)
        self.assertTrue(Path(str(run.run / "summary.json") + bf.SUFFIX).is_file())
        self.assertEqual((run.evidence / "finalizer.stdout.log").read_text().strip(), "finalized")

    def test_openwiki_finalize_refuses_changed_judge_constants(self):
        run, ns, ledger = self.adapter(bf.OpenWiki, "openwiki")
        fos = types.SimpleNamespace(RESULT_JUDGE_MODEL="other", RESULT_JUDGE_EFFORT="max")
        with self.assertRaisesRegex(bf.Refusal, "constants"):
            bf.OpenWiki(run, fos, ns).do_finalize(ledger, [])

    def test_deeptutor_hidden_and_finalize_brokers_as_the_formal_path_starts_them(self):
        run, ns, ledger = self.adapter(bf.DeepTutor, "deeptutor")
        brokers, hidden_calls, stopped = [], [], []

        class BrokerProcess:
            def __init__(self, **kw):
                brokers.append(kw)
                self.endpoint_local = f"http://127.0.0.1:6500{len(brokers)}/v1/responses"

        def run_hidden(manifest, output, **kw):
            hidden_calls.append((manifest, output, kw))
            write(output / "hidden-after-freeze-attestation.json", {"formal_result_eligible": True})
            return {"formal_result_eligible": True}
        fos = types.SimpleNamespace(BrokerProcess=BrokerProcess, run_hidden=run_hidden, HIDDEN_CASES=bf.HIDDEN6,
                                    stop_brokers=lambda items: stopped.append(items) or [{"absent_after_cleanup": True}])
        ad = bf.DeepTutor(run, fos, ns)
        result = ad.do_hidden(ledger, [])
        self.assertEqual(brokers[0], {"run_dir": run.run, "role": "hidden", "credential": run.credential,
                                      "python": sys.executable, "max_calls": 12, "max_tokens": 240_000})
        manifest, output, kw = hidden_calls[0]
        self.assertEqual((manifest, output), (run.lifecycle / "freeze_manifest.json", run.run / "hidden"))
        self.assertEqual(kw, {"broker_endpoint": "http://127.0.0.1:65001/v1/responses",
                              "python_executable": str(Path(ns.runtime_python).absolute()),
                              "case_ids": bf.HIDDEN6, "pilot_not_formal": False})
        self.assertEqual(len(stopped), 1)
        self.assertTrue(result["formal_result_eligible"])
        write(run.run / "summary.json", {"status": "formal_evidence_incomplete", "result_axis": "N/A"})
        ledger.backup(run.run / "summary.json", [])
        write(run.lifecycle / "controller_state.json", {"records": [{"candidate_digest": "a"}], "frozen": {"x": 1}})
        ad.shared_summary_writer = lambda: (lambda r, **kw: write(r / "one_stop_summary.json", {"schema": "v2"}))
        out = ad.do_finalize(ledger, [])
        self.assertEqual(brokers[1], {"run_dir": run.evidence, "role": "result_judge", "credential": run.credential,
                                      "python": sys.executable, "max_calls": 40, "max_tokens": 3_000_000,
                                      "broker_kind": "responses_xhigh"})
        self.assertEqual(out["finalizer_exit"], 0)
        summary = json.loads((run.run / "summary.json").read_text())
        self.assertEqual(summary["status"], "formal_evidence_complete")
        self.assertEqual(summary["result_axis"], {"score": 12.5})
        self.assertEqual(summary["formal_aggregation"], "formal_aggregation.json")

    def test_aider_hidden_starts_a_zero_call_lower_broker_and_cleans_it(self):
        run, ns, ledger = self.adapter(bf.Aider, "aider")
        calls = {}
        fos = types.SimpleNamespace(LOWER_EFFORT="high", BUILDER_EFFORT="max", JUDGE_BROKER_SCRIPT=Path("/c/judge.py"),
                                    port=lambda: 65010)
        fos.start_broker = lambda **kw: calls.setdefault("start", []).append(kw)
        fos.stats = lambda endpoint: {"runtime": {"calls": 0}}
        fos.cleanup_owned_containers = lambda names, attempted, cids: calls.setdefault("cleanup", []).append(
            (names, attempted, cids)) or {"all_attempted_absent": True}
        ad = bf.Aider(run, fos, ns)
        controller = types.SimpleNamespace(frozen={"candidate_digest": "x"},
                                           run_hidden=lambda: [{"case_id": c, "classification": "ok"} for c in bf.HIDDEN6])
        endpoints = []
        ad.controller = lambda endpoint=None: endpoints.append(endpoint) or controller
        result = ad.do_hidden(ledger, [])
        start = calls["start"][0]
        self.assertEqual(start["name"], "aider-formal-hidden-" + ad.suffix())
        self.assertEqual(start["script"], run.tree / "evaluator/broker/lower_responses_broker.py")
        self.assertEqual((start["effort"], start["cidfile"], start["value_port"]),
                         ("high", run.run / "brokers/hidden.cid", 65010))
        self.assertEqual(endpoints, ["http://127.0.0.1:65010/v1/responses"])
        self.assertEqual(calls["cleanup"][0][0], {"hidden": start["name"]})
        self.assertEqual(json.loads((run.run / "hidden_broker_initial.json").read_text())["started_after_freeze"], True)
        self.assertEqual(len(result["cases"]), 6)

    def test_aider_hidden_refuses_a_broker_that_already_has_calls_and_still_cleans_up(self):
        run, ns, ledger = self.adapter(bf.Aider, "aider")
        cleaned = []
        fos = types.SimpleNamespace(LOWER_EFFORT="high", port=lambda: 65011, start_broker=lambda **kw: None,
                                    stats=lambda endpoint: {"runtime": {"calls": 2}},
                                    cleanup_owned_containers=lambda *a: cleaned.append(a) or {})
        with self.assertRaisesRegex(bf.Refusal, "zero calls"):
            bf.Aider(run, fos, ns).do_hidden(ledger, [])
        self.assertEqual(len(cleaned), 1)

    def test_aider_finalize_judge_broker_and_summaries(self):
        run, ns, ledger = self.adapter(bf.Aider, "aider")
        started, cleaned = [], []
        fos = types.SimpleNamespace(LOWER_EFFORT="high", BUILDER_EFFORT="max", JUDGE_BROKER_SCRIPT=Path("/c/judge.py"),
                                    port=lambda: 65012, start_broker=lambda **kw: started.append(kw),
                                    save_role_stats=lambda *a: None,
                                    cleanup_owned_containers=lambda *a: cleaned.append(a) or {})
        ad = bf.Aider(run, fos, ns)
        ad.controller = lambda endpoint=None: types.SimpleNamespace(records=[{"n": 1}], frozen={"d": 1})
        write(run.run / "builder_session_attestation.json", {"builder_session_id": "s"})
        with contextlib.redirect_stdout(io.StringIO()):
            out = ad.do_finalize(ledger, [])
        self.assertEqual(started[0]["script"], Path("/c/judge.py"))
        self.assertEqual((started[0]["effort"], started[0]["cidfile"]), ("max", run.evidence / "result_judge.cid"))
        self.assertEqual(cleaned[0][0], {"result_judge": "aider-formal-result-judge-" + ad.suffix() + "-mf"})
        self.assertEqual(out["finalizer_exit"], 0)
        self.assertEqual(json.loads((run.run / "summary.json").read_text())["status"], "formal_result_ready")
        self.assertEqual(json.loads((run.run / "one_stop_summary.json").read_text())["status"], "completed")

    def test_openwiki_freeze_reason_goes_into_the_sealed_manifest(self):
        """Controller.freeze writes the manifest through its module's write_json; the extras land in it, and the
        module's function is restored afterwards."""
        run, ns, ledger = self.adapter(bf.OpenWiki, "openwiki")
        lifecycle = run.lifecycle
        write(lifecycle / "controller_state.json", {"records": [{"candidate_digest": "c1", "build": {
            "product_entry": str(run.run / "repo" / "bin" / "x")}}]})
        write(run.run / "repo" / "bin" / "x", "x")
        write(run.run / "builder_session_attestation.json", {"builder_session_id": "s", "native_evidence": {
            "valid": False, "errors": [NO_TERMINAL]}, "builder_exit_code": 0})
        write(run.run / "summary.json", {"status": "builder_lifecycle_incomplete"})

        def module_write_json(path, value):
            write(Path(path), value)
        controller_module = types.ModuleType("agentloop.evaluator.controller")
        controller_module.write_json = module_write_json

        class Controller:
            frozen, records = None, [{"candidate_digest": "c1"}]

            def freeze(self, *, builder_exit_evidence=None):
                manifest = {"candidate_digest": "f1", "repository_digest": "R", "builder_exit_evidence": builder_exit_evidence}
                controller_module.write_json(lifecycle / "freeze_manifest.json", manifest)
                write(lifecycle / "controller_state.json", {"records": self.records, "frozen": manifest})
                return manifest
        hidden_module = types.ModuleType("agentloop.evaluator.hidden_controller")
        hidden_module.validate_freeze = lambda manifest, run_dir, path: path
        protocol = types.ModuleType("agentloop.protocol")
        protocol.tree_digest = lambda path: "R"
        modules = {"agentloop": types.ModuleType("agentloop"), "agentloop.evaluator": types.ModuleType("agentloop.evaluator"),
                   "agentloop.evaluator.controller": controller_module,
                   "agentloop.evaluator.hidden_controller": hidden_module, "agentloop.protocol": protocol}
        modules["agentloop"].evaluator, modules["agentloop"].protocol = modules["agentloop.evaluator"], protocol
        modules["agentloop.evaluator"].controller = controller_module
        modules["agentloop.evaluator"].hidden_controller = hidden_module
        ad = bf.OpenWiki(run, types.SimpleNamespace(), ns)
        ad.controller = lambda endpoint=None: Controller()
        with mock.patch.dict(sys.modules, modules):
            result = ad.do_freeze(ledger, [])
        manifest = json.loads((lifecycle / "freeze_manifest.json").read_text())
        self.assertEqual((manifest["freeze_reason"], manifest["manual_freeze"]), (bf.REASON, True))
        self.assertEqual(manifest["builder_exit_evidence"]["harbor_exception"]["exception_type"], "AgentTimeoutError")
        self.assertEqual(manifest["builder_exit_evidence"]["native_errors"], [NO_TERMINAL])
        self.assertIs(controller_module.write_json, module_write_json)
        self.assertEqual(result["frozen"]["candidate_digest"], "f1")
        att = json.loads((run.run / "builder_session_attestation.json").read_text())
        self.assertEqual(att["freeze"]["freeze_reason"], bf.REASON)
        self.assertEqual(att["manual_freeze"]["reason"], bf.REASON)
        backups = {b["path"] for b in json.loads(run.record_path.read_text())["backups"]}
        self.assertEqual(backups, {str(lifecycle / "controller_state.json"),
                                   str(run.run / "builder_session_attestation.json"), str(run.run / "summary.json")})


# ------------------------------------------------------------------------- agreement with `agentswe result` --
DEADLINE = 1_800_018_000.0


def promoted_cut(run: Path, **kw) -> None:
    """A one-segment receipt as control/builder_segments.py writes it (the segment promoted), ended by the budget."""
    budget_cut(run, **kw)
    value = json.loads((run / "builder_segment_receipt.json").read_text())
    value["attempts"][0].update(promoted=True, trial="builder_task__abc", started_at_epoch=900.0)
    value["budget_seconds"] = 18000.0
    write(run / "builder_segment_receipt.json", value)


class TreeLayouts:
    """Minimal files each supported tree writes at its lifecycle gate after a budget cut (the paths the tool's
    adapters and editing_agentloop_v1.budget_cut_state read)."""

    @staticmethod
    def sources(run, n):
        records = []
        for i in range(1, n + 1):
            write(run / "lifecycle" / f"candidate_{i}" / "f", f"candidate {i}")
            records.append({"round": i, "submission_number": i, "session_id": "s",
                            "candidate_digest": hashlib.sha256(f"candidate {i}".encode()).hexdigest(),
                            "candidate_path": str(run / "lifecycle" / f"candidate_{i}"), "accepted": True,
                            "round_consumed": True, "state": "completed",
                            "materialized_path": str(run / "lifecycle" / f"candidate_{i}")})
        return records

    @classmethod
    def openwiki(cls, run, n, errors):
        records = cls.sources(run, n)
        write(run / "lifecycle" / "controller_state.json", {"records": records, "frozen": None})
        write(run / "builder_session_attestation.json", {"native_evidence": {"valid": False, "errors": errors},
                                                         "candidate_records": records, "builder_exit_code": 0,
                                                         "freeze": None})
        write(run / "summary.json", {"status": "builder_lifecycle_incomplete", "builder_session_attestation": {}})

    @classmethod
    def deeptutor(cls, run, n, errors):
        records = cls.sources(run, n)
        write(run / "lifecycle" / "controller_state.json", {"records": records, "frozen": None})
        write(run / "builder_native_attestation.json", {"valid": False, "errors": errors, "builder_exit_code": 0})
        write(run / "builder_process.json", {"exit_code": 0, "lifecycle_ready_for_hidden": False})
        write(run / "summary.json", {"status": "formal_evidence_incomplete", "builder_exit_code": 0})

    @classmethod
    def aider(cls, run, n, errors):
        records = cls.sources(run, n)
        frozen = {"candidate_delivery_digest": records[-1]["candidate_digest"],
                  "candidate_materialized_digest": hashlib.sha256(f"candidate {n}".encode()).hexdigest(),
                  "freeze_reason": "builder_exit"} if records else None
        write(run / "lifecycle" / "dev_lifecycle.json", {"records": records, "frozen": frozen})
        write(run / "builder_session_attestation.json", {"same_session": False, "native_evidence": {
            "valid": False, "errors": errors}, "candidate_records": records, "builder_exit_code": 0, "freeze": frozen})
        write(run / "summary.json", {"status": "builder_integration_incomplete", "builder_exit_code": 0})


def file_level(cls):
    """The real adapter with its tree-code checks and digest replaced (the agreement is about the run's files)."""
    class Adapter(cls):
        def tree_gates(self, stage, facts):
            return {}

        def digest(self, path):
            return hashlib.sha256((Path(path) / "f").read_bytes()).hexdigest()
    return Adapter


class ResultAgreement(ToolBase):
    """`agentswe result`'s budget_exhausted block (budget_cut_state) and `freeze --stage check` agree on whether a run
    is a budget cut, how many submissions were accepted and which one is the latest."""
    CASES = (("openwiki", bf.OpenWiki), ("deeptutor", bf.DeepTutor), ("aider", bf.Aider))
    TRIGGER = ("Builder cut at the budget", "stored strict native proof", "summary status is", "1..5 accepted",
               "accepted submission digests distinct", "latest accepted source", "no held-out evidence",
               "no formal_aggregation.json")

    def build(self, task, n=2, errors=(NO_TERMINAL,), **cut):
        run_dir = self.home.run(task)
        promoted_cut(run_dir, **cut)
        getattr(TreeLayouts, task)(run_dir, n, list(errors))
        return run_dir

    def compare(self, task, cls, run_dir):
        state = ed.budget_cut_state(run_dir, task=task, unit_state="failed")
        run = bf.Run(run_dir, control=self.home.control)
        ad = file_level(cls)(run, types.SimpleNamespace(), argparse.Namespace(max_dev_rounds=5))
        g, facts = bf.trigger_gates(run, ad, "freeze", bf.Ledger(run))
        trigger = {name: ok for name, ok in g.items() if name.startswith(self.TRIGGER)}
        return state, trigger, facts, run, ad

    def test_budget_cut_runs_agree(self):
        for task, cls in self.CASES:
            with self.subTest(task=task):
                state, trigger, facts, run, ad = self.compare(task, cls, self.build(task, n=3))
                self.assertIsNotNone(state)
                self.assertTrue(all(trigger.values()), trigger)
                self.assertEqual(state["accepted_submissions"], facts["accepted_submissions"])
                self.assertEqual(state["latest_accepted"]["digest"], facts["latest_accepted"]["candidate_digest"])
                run.result_state = cli_freeze.result_state(run.run, task, "failed")
                g, _ = bf.trigger_gates(run, ad, "freeze", bf.Ledger(run))
                agree = {k: v for k, v in g.items() if "agentswe result" in k}
                self.assertEqual(len(agree), 3, agree)
                self.assertTrue(all(agree.values()), agree)

    def test_runs_that_are_not_a_budget_cut_are_refused_by_both(self):
        variants = ({"resume": True}, {"exit_reason": "completed"}, {"exception": None, "before_deadline": 3600.0},
                    {"errors": (NO_TERMINAL, "feedback response has no successful socket-write receipt")})
        for task, cls in self.CASES:
            for kw in variants:
                with self.subTest(task=task, **{k: str(v) for k, v in kw.items()}):
                    for path in sorted(self.home.formal.rglob("*"), reverse=True):
                        path.unlink() if path.is_file() else path.rmdir()
                    errors = kw.get("errors", (NO_TERMINAL,))
                    run_dir = self.build(task, errors=errors, **{k: v for k, v in kw.items() if k != "errors"})
                    state, trigger, _, run, ad = self.compare(task, cls, run_dir)
                    self.assertIsNone(state)
                    self.assertFalse(all(trigger.values()), trigger)
                    run.result_state = cli_freeze.result_state(run.run, task, "failed")
                    g, _ = bf.trigger_gates(run, ad, "freeze", bf.Ledger(run))
                    self.assertIs(g["agentswe result reports this run as cut by the Builder budget"], False)

    def test_held_out_evidence_ends_both(self):
        run_dir = self.build("openwiki")
        write(run_dir / "lifecycle" / "hidden-result.json", {})
        state, trigger, _, _, _ = self.compare("openwiki", bf.OpenWiki, run_dir)
        self.assertIsNone(state)
        self.assertFalse(trigger["no held-out evidence yet"])

    def test_zero_accepted_scores_0_in_result_and_is_refused_by_freeze(self):
        run_dir = self.build("deeptutor", n=0)
        state, _, facts, run, ad = self.compare("deeptutor", bf.DeepTutor, run_dir)
        self.assertEqual(state["accepted_submissions"], 0)
        self.assertEqual(ed.budget_exhausted_report(state, "e-x")["score"], 0)
        with self.assertRaisesRegex(bf.Refusal, "Result is 0"):
            bf.stage_run(run, ad, bf.Ledger(run), "freeze", False, "op", {}, [])

    def test_a_disagreeing_report_refuses_the_freeze(self):
        run_dir = self.build("openwiki", n=2)
        _, _, _, run, ad = self.compare("openwiki", bf.OpenWiki, run_dir)
        run.result_state = {**cli_freeze.result_state(run.run, "openwiki", "failed"), "accepted_submissions": 1}
        g, _ = bf.trigger_gates(run, ad, "freeze", bf.Ledger(run))
        self.assertIs(g["accepted submissions agree with agentswe result"], False)

    def test_the_supported_tasks_are_one_list(self):
        self.assertEqual(set(bf.SUPPORTED), set(ed.BUDGET_FREEZE_TASKS))
        self.assertEqual(set(cli_freeze.SUPPORTED), set(ed.BUDGET_FREEZE_TASKS))
        state = {"task": "openwiki", "accepted_submissions": 2, "latest_accepted": {"submission": 2},
                 "builder": {}, "status": "builder_lifecycle_incomplete", "summary": "summary.json"}
        self.assertTrue(ed.budget_exhausted_report(state, "e-x")["next"].startswith("agentswe freeze e-x"))
        self.assertIn("not yet available", ed.budget_exhausted_report({**state, "task": "codex"}, "e-x")["next"])


# ------------------------------------------------------------------------------------------------- CLI side --
class Role:
    api_key, wire, base_url, model, effort = "credential-value-for-tests", "responses", "https://p.example/v1", "m", "e"


class Cfg:
    def __init__(self, home):
        self.home = home
        self.profile = "lite"
        self.values = {}

    def role(self, name, family=None):
        return Role()

    def get(self, key, default=None):
        return default


class CliSide(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.run_dir = self.home / "runs" / "editing" / "formal" / "codex_xhigh" / "deeptutor" / "r"
        self.run_dir.mkdir(parents=True)
        self.launch = {"run_id": "e-deeptutor-oss-formal-s1-x", "task": "deeptutor-adaptive-remediation",
                       "family": "editing", "mode": "formal", "unit": "agentswe-oss-formal-deeptutor-x.service",
                       "run_dir": str(self.run_dir)}
        patcher = mock.patch.object(ed, "_unit_state", return_value="failed")
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, launch=None, **kw):
        seen = {}

        def fake_run(argv, check=False):
            seen["argv"] = argv
            cred = ed.credential_file(Cfg(self.home))
            seen["credential_at_call"] = cred.is_file() and stat.S_IMODE(cred.stat().st_mode)
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(cli_freeze.subprocess, "run", side_effect=fake_run):
            code = cli_freeze.freeze(Cfg(self.home), launch or self.launch, **kw)
        return code, seen

    def test_other_families_smoke_runs_and_unsupported_tasks_refuse(self):
        for launch, text in (({**self.launch, "family": "creation"}, "formal Editing runs only"),
                             ({**self.launch, "mode": "smoke"}, "formal runs only")):
            with self.assertRaises(SystemExit) as caught:
                cli_freeze.freeze(Cfg(self.home), launch)
            self.assertIn(text, str(caught.exception))
        others = [tid for tid, key in ed.TASK_KEYS.items() if key not in cli_freeze.SUPPORTED]
        self.assertEqual(len(others), 7)
        for tid in others:
            with self.assertRaises(SystemExit) as caught:
                cli_freeze.freeze(Cfg(self.home), {**self.launch, "task": tid})
            self.assertIn("does not support", str(caught.exception))
            self.assertIn("yet", str(caught.exception))

    def test_live_unit_missing_run_dir_and_other_owner_refuse(self):
        with mock.patch.object(ed, "_unit_state", return_value="active"):
            with self.assertRaisesRegex(SystemExit, "still running"):
                cli_freeze.freeze(Cfg(self.home), self.launch)
        with self.assertRaisesRegex(SystemExit, "missing"):
            cli_freeze.freeze(Cfg(self.home), {**self.launch, "run_dir": str(self.home / "nope")})
        with mock.patch.object(cli_freeze.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(SystemExit, "owner"):
                cli_freeze.freeze(Cfg(self.home), self.launch)

    def test_argv_and_tool_render(self):
        code, seen = self.invoke(stage="freeze", apply=True, operator="ops")
        argv = seen["argv"]
        self.assertEqual(code, 0)
        self.assertEqual(argv[:4], ["/usr/bin/python3", "-E", "-s", "-B"])
        self.assertEqual(argv[5:9], ["--run-dir", str(self.run_dir), "--stage", "freeze"])
        self.assertEqual(argv[9], "--result-state")
        self.assertEqual(json.loads(argv[10]), {"detected": False})  # an empty run dir is no budget cut for result
        self.assertEqual(argv[11:], ["--apply", "--operator", "ops"])
        self.assertTrue(argv[4].endswith("budget_freeze.py"))
        installed = self.home / "editing" / "tools"
        for name in cli_freeze.TOOL_FILES:
            write(installed / name, "# installed\n")
        _, seen = self.invoke()
        self.assertEqual(seen["argv"][4], str(installed / "budget_freeze.py"))
        self.assertEqual(seen["argv"][5:9], ["--run-dir", str(self.run_dir), "--stage", "check"])
        self.assertEqual(seen["argv"][9], "--result-state")
        self.assertEqual(len(seen["argv"]), 11)

    def test_fresh_render_replaces_every_token(self):
        with tempfile.TemporaryDirectory() as scratch:
            tool = cli_freeze.tool_path(Cfg(self.home), ed.tokens(Cfg(self.home)), Path(scratch))
            for name in cli_freeze.TOOL_FILES:
                self.assertNotIn("@@AGENTSWE_", (tool.parent / name).read_text())
            self.assertIn(str(self.home / "editing" / "control"), tool.read_text())

    def test_credential_only_for_an_apply_of_hidden_finalize_or_all_and_removed_after(self):
        cred = ed.credential_file(Cfg(self.home))
        for stage, apply, expected in (("check", False, False), ("freeze", True, False), ("hidden", False, False),
                                       ("hidden", True, 0o600), ("finalize", True, 0o600), ("all", True, 0o600)):
            with self.subTest(stage=stage, apply=apply):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    _, seen = self.invoke(stage=stage, apply=apply)
                self.assertEqual(seen["credential_at_call"], expected)
                self.assertFalse(cred.exists())
                self.assertNotIn(Role.api_key, out.getvalue())

    def test_credential_kept_while_another_run_of_the_home_is_live(self):
        other = {"run_id": "e-aider-other", "unit": "agentswe-oss-formal-aider-y.service"}
        write(self.home / "runs" / "editing" / "e-aider-other.launch.json", other)
        with mock.patch.object(ed, "_unit_state", side_effect=lambda unit: "active" if unit == other["unit"] else "failed"):
            self.invoke(stage="hidden", apply=True)
        self.assertTrue(ed.credential_file(Cfg(self.home)).is_file())

    def test_cli_routes_freeze_to_the_runner_and_refuses_other_families(self):
        from agentswe import cli
        write(self.home / "runs" / "creation" / "c-x.launch.json", {"run_id": "c-x", "task": "web-research-report",
                                                                    "family": "creation"})
        with mock.patch.object(cli.config, "load", return_value=Cfg(self.home)):
            with self.assertRaisesRegex(SystemExit, "formal Editing runs only"):
                cli.main(["freeze", "c-x"])
            write(self.home / "runs" / "editing" / "e-d.launch.json", self.launch | {"run_id": "e-d"})
            with mock.patch.object(ed, "freeze", return_value=0) as called:
                self.assertEqual(cli.main(["freeze", "e-d", "--stage", "hidden", "--apply"]), 0)
            self.assertEqual(called.call_args.kwargs, {"stage": "hidden", "apply": True, "operator": None})


if __name__ == "__main__":
    unittest.main()
