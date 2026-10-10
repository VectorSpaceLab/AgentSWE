"""Docker 29 removes an exited or stopped --rm container asynchronously: for several seconds `docker inspect` (and
`docker ps -a`) still show it and `docker rm -f` exits 1 with "removal of container ... is already in progress".
The evaluator cleanups inside the Editing task trees wait for that removal before judging absence; any other rm
refusal is judged as before. A fake `docker` on PATH reproduces the race (stdlib unittest, no Docker needed).

Also: Claude's classify_incomplete names a run that ended unfrozen because its last submission hit a recorded
public/launcher infrastructure failure as that failure, not as a Builder freeze-gate failure."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITING = ROOT / "tasks" / "editing"
CONTROL = ROOT / "runners" / "editing" / "control"
BUSY = "Error response from daemon: removal of container {} is already in progress"

FAKE_DOCKER = r'''
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path))
argv = sys.argv[1:]
state.setdefault("calls", []).append(argv)
containers = state["containers"]

def save():
    json.dump(state, open(state_path, "w"))

def find(ref):
    for cid, c in containers.items():
        if ref in (cid, c.get("name"), "/" + str(c.get("name"))) or (len(ref) >= 12 and cid.startswith(ref)):
            return cid
    return None

def observe(cid):
    """A container the daemon is removing is shown for `polls` more observations, then it is gone."""
    c = containers[cid]
    if c["status"] != "removing":
        return True
    if c["polls"] > 0:
        c["polls"] -= 1
        return True
    del containers[cid]
    return False

def view(cid):
    c = containers[cid]
    running = c["status"] == "running"
    return {"Id": cid, "Name": "/" + c.get("name", cid[:12]), "Image": c.get("image", "sha256:" + "0" * 64),
            "State": {"Status": c["status"], "Running": running, "Pid": 4242 if running else 0, "ExitCode": 0},
            "Config": {"Labels": c.get("labels", {})},
            "HostConfig": {"AutoRemove": c.get("autoremove", True), "CgroupParent": c.get("cgroup_parent", "")},
            "Mounts": c.get("mounts", [])}

code, out, err = 0, "", ""
cmd = argv[0] if argv else ""
if cmd == "inspect":
    fmt = None
    rest = argv[1:]
    if rest[:1] == ["--format"]:
        fmt, rest = rest[1], rest[2:]
    cid = find(rest[0])
    if cid is None or not observe(cid):
        code, err = 1, "Error: No such object: " + rest[0] + "\n"
    elif fmt == "{{json .Config.Labels}}":
        out = json.dumps(containers[cid].get("labels", {})) + "\n"
    elif fmt == "{{.Id}}":
        out = cid + "\n"
    else:
        out = json.dumps([view(cid)]) + "\n"
elif cmd == "rm":
    ref = [a for a in argv[1:] if not a.startswith("-")][0]
    cid = find(ref)
    if cid is None:
        err = "Error response from daemon: No such container: " + ref + "\n"  # rm -f exits 0 here (Docker 24 and 29)
    elif state.get("rm_refusal"):
        code, err = 1, state["rm_refusal"] + "\n"
    elif containers[cid]["status"] == "removing":
        code, err = 1, "Error response from daemon: removal of container " + cid + " is already in progress\n"
    else:
        del containers[cid]
        out = ref + "\n"
elif cmd in ("stop", "kill"):
    cid = find(argv[-1])
    if cid is None:
        code, err = 1, "Error response from daemon: No such container: " + argv[-1] + "\n"
    else:
        c = containers[cid]
        c["status"] = "removing" if c.get("autoremove", True) else "exited"
        out = argv[-1] + "\n"
elif cmd == "ps":
    filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
    full = "--no-trunc" in argv
    rows = []
    for cid in list(containers):
        c = containers[cid]
        ok = True
        for f in filters:
            key, _, value = f.partition("=")
            if key == "label":
                k, _, v = value.partition("=")
                ok = ok and c.get("labels", {}).get(k) == v
            elif key == "name":
                ok = ok and value.strip("^$") == c.get("name")
        if ok and observe(cid):
            rows.append(cid if full else cid[:12])
    out = "".join(r + "\n" for r in rows)
elif cmd == "create":
    cidfile = argv[argv.index("--cidfile") + 1]
    cid = state["create_id"]
    labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
    parent = argv[argv.index("--cgroup-parent") + 1] if "--cgroup-parent" in argv else ""
    containers[cid] = {"status": "created", "autoremove": "--rm" in argv, "polls": state.get("create_polls", 3),
                       "labels": labels, "cgroup_parent": parent}
    open(cidfile, "w").write(cid)
    out = cid + "\n"
elif cmd == "start":
    cid = find(argv[-1])
    containers[cid]["status"] = "removing" if containers[cid]["autoremove"] else "exited"  # exits at once
    code = 78
save()
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
'''


def render_tree(task: str, scratch: Path) -> Path:
    """A private copy of a task tree with the release placeholders filled in."""
    target = scratch / task
    shutil.copytree(EDITING / task / "tree", target, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".runtime"))
    tokens = {"@@AGENTSWE_EDITING_CONTROL@@": str(CONTROL)}
    for path in target.rglob("*.py"):
        if path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        if "@@AGENTSWE_" in text:
            for token, value in tokens.items():
                text = text.replace(token, value)
            path.write_text(text, encoding="utf-8", errors="surrogateescape")
    return target


def load(tree: Path, relative: str, name: str):
    """Import one tree module the way its scripts run (tree root and own directory on sys.path)."""
    path = tree / relative
    before_path, before_modules = list(sys.path), set(sys.modules)
    sys.path[:0] = [str(path.parent), str(tree), str(CONTROL)]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before_path
        for key in set(sys.modules) - before_modules - {name}:
            sys.modules.pop(key, None)  # tree packages (harbor, evaluator, ...) differ per tree
    for attribute in ("REMOVAL_POLL_SECONDS",):
        if hasattr(module, attribute):
            setattr(module, attribute, 0.01)
    return module


class FakeDockerCase(unittest.TestCase):
    TASK = ""
    MODULES: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="k-rmrace-"))
        tree = render_tree(cls.TASK, cls.scratch / "trees")
        cls.mod = {key: load(tree, rel, f"k_{cls.__name__}_{key}") for key, rel in cls.MODULES.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.scratch))
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        self.state_path = self.work / "docker_state.json"
        self.saved_env = {key: os.environ.get(key) for key in ("PATH", "FAKE_DOCKER_STATE")}
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_DOCKER_STATE"] = str(self.state_path)

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def docker(self, containers: dict, **extra):
        self.state_path.write_text(json.dumps({"containers": containers, **extra}))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def cidfile(self, cid: str) -> Path:
        path = self.work / (cid[:8] + ".cid")
        path.write_text(cid + "\n")
        return path

    @staticmethod
    def removing(polls: int, **fields) -> dict:
        return {"status": "removing", "autoremove": True, "polls": polls, **fields}


CID = "c" * 64


class ClaudePolicyProvenance(FakeDockerCase):
    TASK = "claude-policy-provenance"
    MODULES = {"product": "agentloop/evaluator/product_lifecycle.py", "harbor": "harbor/formal_one_stop.py"}
    OWNER = "claude-" + "5" * 32

    def product(self, **fields):
        return {CID: {"labels": {"agentswe.claude.case-owner": self.OWNER}, **fields}}

    def test_exited_product_being_removed_is_awaited(self):
        # The 2026-10-09 smoke: rm -f exit 1 "already in progress", inspect still finds the product.
        self.docker(self.product(**self.removing(5)))
        result = self.mod["product"].cleanup_owned(self.OWNER, deadline=None)
        record = result["records"][0]
        self.assertTrue(result["complete"], result)
        self.assertIs(record["absent_after_cleanup"], True)
        self.assertEqual(record["remove_exit_code"], 1)
        self.assertIs(record["removal_in_progress_at_rm"], True)
        self.assertIn("already in progress", record["remove_stderr"])

    def test_wait_stays_inside_the_case_cleanup_deadline(self):
        import time
        self.docker(self.product(**self.removing(10_000)))
        started = time.monotonic()
        result = self.mod["product"].cleanup_owned(self.OWNER, deadline=started + 7)
        self.assertLess(time.monotonic() - started, 7)
        self.assertFalse(result["complete"])
        self.assertIs(result["records"][0]["absent_after_cleanup"], False)

    def test_other_rm_refusal_still_fails(self):
        self.docker(self.product(status="exited", autoremove=False, polls=0),
                    rm_refusal="Error response from daemon: cannot remove container: permission denied")
        result = self.mod["product"].cleanup_owned(self.OWNER, deadline=None)
        self.assertFalse(result["complete"])
        self.assertIs(result["records"][0]["absent_after_cleanup"], False)
        self.assertFalse(result["records"][0].get("removal_in_progress_at_rm"))

    def test_container_that_never_goes_away_is_not_absent(self):
        module = self.mod["product"]
        saved, module.REMOVAL_WAIT_SECONDS = module.REMOVAL_WAIT_SECONDS, 0.2
        try:
            self.docker(self.product(**self.removing(10_000)))
            result = module.cleanup_owned(self.OWNER, deadline=None)
        finally:
            module.REMOVAL_WAIT_SECONDS = saved
        self.assertFalse(result["complete"])
        self.assertIs(result["records"][0]["absent_after_cleanup"], False)

    def test_broker_cleanup_waits_for_removal(self):
        self.docker({CID: self.removing(3)})
        result = self.mod["harbor"].cleanup_owned_container("public_lower", self.cidfile(CID), attempted=True)
        self.assertIs(result["absent_after_cleanup"], True, result)
        self.assertEqual(result["status"], "absent")
        self.assertIs(result["removal_in_progress_at_rm"], True)

    def test_broker_cleanup_running_container_unchanged(self):
        self.docker({CID: {"status": "running", "autoremove": True, "polls": 0}})
        result = self.mod["harbor"].cleanup_owned_container("public_lower", self.cidfile(CID), attempted=True)
        self.assertIs(result["absent_after_cleanup"], True)
        self.assertNotIn("removal_in_progress_at_rm", result)

    def test_run_mounted_cleanup_waits_for_removal(self):
        run_dir = self.work / "run"
        run_dir.mkdir()
        self.docker({CID: self.removing(4, name="product", mounts=[{"Source": str(run_dir / "workspace")}])})
        rows = self.mod["harbor"].cleanup_run_mounted_containers(run_dir, set())
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["absent_after_cleanup"], True, rows)


class ClaudeIncompleteClassification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="k-classify-"))
        tree = render_tree("claude-policy-provenance", cls.scratch)
        cls.harbor = load(tree, "harbor/formal_one_stop.py", "k_claude_classify")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def lifecycle(self, events, *, rounds=1, frozen=None):
        controller = types.SimpleNamespace(rounds=[{}] * rounds, frozen=frozen)
        return types.SimpleNamespace(controller=controller, events=events, run_dir=self.scratch)

    INFRA = {"event": "submission_not_consumed", "classification": "public_or_launcher_infrastructure_failure"}
    ACCEPTED = {"event": "submission_finished", "accepted": True, "submission_consumed": True}

    def test_unfrozen_after_infrastructure_failure_is_infrastructure(self):
        # The 2026-10-09 smoke: round 1 accepted; round 2 failed in product cleanup; its resubmission was refused.
        events = [self.ACCEPTED, {"event": "submission_started"}, dict(self.INFRA, error_type="OSError"),
                  {"event": "submission_started"}, dict(self.INFRA, error_type="RuntimeError"),
                  {"event": "builder_invocation_finished", "exit_code": 0}]
        self.assertEqual(self.harbor.classify_incomplete(0, self.lifecycle(events)),
                         "public_or_launcher_infrastructure_failure")

    def test_accepted_submission_after_the_failure_keeps_the_freeze_gate_label(self):
        events = [self.ACCEPTED, self.INFRA, self.ACCEPTED]
        self.assertEqual(self.harbor.classify_incomplete(0, self.lifecycle(events, rounds=2)),
                         "latest_candidate_freeze_gate_failure")

    def test_no_infrastructure_event_keeps_the_freeze_gate_label(self):
        self.assertEqual(self.harbor.classify_incomplete(0, self.lifecycle([self.ACCEPTED])),
                         "latest_candidate_freeze_gate_failure")

    def test_other_branches_unchanged(self):
        self.assertEqual(self.harbor.classify_incomplete(124, self.lifecycle([self.INFRA])),
                         "builder_timeout_or_no_submission")
        self.assertEqual(self.harbor.classify_incomplete(0, self.lifecycle([self.INFRA], rounds=0)),
                         "builder_no_submission")
        self.assertEqual(self.harbor.classify_incomplete(0, self.lifecycle([self.INFRA], frozen={"x": 1})),
                         "builder_lifecycle_incomplete")


class AiScientistReproducibilityGate(FakeDockerCase):
    TASK = "ai-scientist-reproducibility-gate"
    MODULES = {"gate": "agentloop/product_resource_gate.py", "replay": "evaluator/immutable_replay.py",
               "harbor": "harbor/formal_one_stop.py"}
    PARENT = "agentswe_ai_scientist_" + "a" * 32 + ".slice"

    def test_gate_records_absence_after_daemon_removal(self):
        # The product (--rm) exits before cgroup verification; cleanup meets it while the daemon removes it.
        self.docker({}, create_id=CID, create_polls=4)
        command = ["docker", "run", "--rm", "--name", "p", "--cgroup-parent", self.PARENT, "img", "python3", "-c", "0"]
        output = self.work / "action_001_runtime"
        with self.assertRaises(OSError):
            self.mod["gate"].run_gated_product(command, output=output, parent=self.PARENT, timeout=30)
        cleanup = json.loads((output / "resource_cleanup.json").read_text())
        self.assertIs(cleanup["container_absent"], True, cleanup)
        self.assertIs(cleanup["removal_in_progress_at_rm"], True)

    def test_replay_cleanup_awaits_removal(self):
        owner = "0" * 20
        labels = {"agentswe.owner": "0909-owner-b", "agentswe.run_id": owner}
        self.docker({CID: self.removing(5, labels=labels)})
        report = self.mod["replay"].cleanup_owned_runtime(owner)
        self.assertTrue(report["all_absent"], report)
        self.assertNotIn("error", report)

    def test_replay_cleanup_container_gone_before_inspect(self):
        owner = "0" * 20
        labels = {"agentswe.owner": "0909-owner-b", "agentswe.run_id": owner}
        self.docker({CID: self.removing(1, labels=labels)})  # listed once, gone at inspect
        report = self.mod["replay"].cleanup_owned_runtime(owner)
        self.assertTrue(report["all_absent"], report)

    def test_replay_other_refusal_still_fails(self):
        owner = "0" * 20
        labels = {"agentswe.owner": "0909-owner-b", "agentswe.run_id": owner}
        self.docker({CID: {"status": "exited", "autoremove": False, "polls": 0, "labels": labels}},
                    rm_refusal="Error response from daemon: device or resource busy")
        report = self.mod["replay"].cleanup_owned_runtime(owner)
        self.assertFalse(report["all_absent"])
        self.assertEqual(report["error"], "owned container removal unconfirmed")

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        receipt = self.mod["harbor"].remove_and_verify_container("lower", self.cidfile(CID), attempted=True)
        self.assertIs(receipt["absent_after_cleanup"], True, receipt)


class AiderWorktreeTransaction(FakeDockerCase):
    TASK = "aider-worktree-transaction"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        result = self.mod["harbor"].cleanup_owned_containers({"public": "n"}, {"public": True},
                                                             {"public": self.cidfile(CID)})
        self.assertTrue(result["all_attempted_absent"], result)
        self.assertIs(result["containers"]["public"]["removal_in_progress_at_rm"], True)


class CodexExecutionResidual(FakeDockerCase):
    TASK = "codex-execution-residual"
    MODULES = {"lower": "evaluator/harness/run_lower_agent_case.py", "harbor": "harbor/formal_one_stop.py"}

    def test_killed_lower_container_is_awaited(self):
        # run_turn kills the --rm lower container at the deadline; Docker 29 is still removing it.
        owner = "owner-1"
        self.docker({CID: self.removing(4, name="codex-lower", labels={"agentswe.codex.lower-owner": owner})})
        record = self.mod["lower"].cleanup_lower_container("codex-lower", owner)
        self.assertIs(record["absent_after_cleanup"], True, record)
        self.assertIs(record["removal_in_progress_at_rm"], True)

    def test_lower_wait_is_bounded_by_the_caller(self):
        owner = "owner-1"
        self.docker({CID: self.removing(10_000, name="codex-lower", labels={"agentswe.codex.lower-owner": owner})})
        record = self.mod["lower"].cleanup_lower_container("codex-lower", owner, wait_seconds=0.1)
        self.assertIs(record["absent_after_cleanup"], False)

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        record = self.mod["harbor"].cleanup_broker(role="public", name="b", port=9, attempted=True,
                                                   run_dir=self.work, cidfile=self.cidfile(CID))
        self.assertIs(record["absent_after_cleanup"], True, record)


class DeepcodeClaimTraceability(FakeDockerCase):
    TASK = "deepcode-claim-traceability"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        record = self.mod["harbor"].remove_and_verify_broker("lower", CID)
        self.assertIs(record["absent_after_cleanup"], True, record)


class DeeptutorAdaptiveRemediation(FakeDockerCase):
    TASK = "deeptutor-adaptive-remediation"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_stopped_broker_removal_is_awaited(self):
        broker = object.__new__(self.mod["harbor"].BrokerProcess)
        broker.__dict__.update(role="public", run_dir=self.work, container_id=CID, container_name="b",
                               defer_removal=False, broker_kind="lower", process=None, stdout=None, stderr=None,
                               endpoint_local="http://127.0.0.1:9/v1/responses")
        self.docker({CID: self.removing(3)})
        receipt = broker.stop()
        self.assertIs(receipt["absent_after_cleanup"], True, receipt)
        self.assertIs(receipt["removal_in_progress_at_rm"], True)


class DyadAcceptanceDriven(FakeDockerCase):
    TASK = "dyad-acceptance-driven"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        record = self.mod["harbor"].remove_container("lower", CID)
        self.assertIs(record["absent_after_cleanup"], True, record)
        self.assertIn("already in progress", record["remove_stderr"])


class OpenclawChannelHandoff(FakeDockerCase):
    TASK = "openclaw-channel-handoff"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        record = self.mod["harbor"].cleanup_owned_container("public_lower", self.cidfile(CID), attempted=True)
        self.assertIs(record["absent_after_cleanup"], True, record)


class OpenhandsEffectRecovery(FakeDockerCase):
    TASK = "openhands-effect-recovery"
    MODULES = {"harbor": "harbor/formal_one_stop.py"}

    def test_retained_rm_broker_is_awaited_after_stop(self):
        # Readiness retention stops the --rm lower broker; Docker 29 shows it as "removing", then drops it.
        self.docker({CID: {"status": "running", "autoremove": True, "polls": 3}})
        receipt = self.mod["harbor"].retain_owned_container("public_lower", self.cidfile(CID), attempted=True)
        self.assertIs(receipt["absent_after_cleanup"], True, receipt)
        self.assertEqual(receipt["status"], "absent")

    def test_retained_deferred_container_is_still_retained(self):
        self.docker({CID: {"status": "running", "autoremove": False, "polls": 0}})
        receipt = self.mod["harbor"].retain_owned_container("judge", self.cidfile(CID), attempted=True)
        self.assertEqual(receipt["status"], "retained")
        self.assertNotIn("removal_in_progress_after_stop", receipt)

    def test_broker_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        receipt = self.mod["harbor"].cleanup_owned_container("public_lower", self.cidfile(CID), attempted=True)
        self.assertIs(receipt["absent_after_cleanup"], True, receipt)


class OpenwikiChangeImpact(FakeDockerCase):
    TASK = "openwiki-change-impact"
    MODULES = {"broker": "agentloop/evaluator/broker.py", "harbor": "harbor/formal_one_stop.py"}

    def test_broker_close_awaits_removal(self):
        broker = object.__new__(self.mod["broker"].EvaluatorBrokerLifecycle)
        recorded = {}
        broker.__dict__.update(_closed=False, defer_removal=False, process=None, _stdout=None, _stderr=None,
                               container_id=CID, container_cidfile=self.cidfile(CID),
                               stats_path=self.work / "missing.json")
        broker._record = lambda **values: recorded.update(values)
        self.docker({CID: self.removing(3)})
        broker.close()
        self.assertIs(recorded["container_absent"], True, recorded)
        self.assertEqual(recorded["cleanup_errors"], [])
        self.assertIn("already in progress", recorded["removal_in_progress_at_rm"])

    def test_broker_close_other_refusal_is_still_an_error(self):
        broker = object.__new__(self.mod["broker"].EvaluatorBrokerLifecycle)
        recorded = {}
        broker.__dict__.update(_closed=False, defer_removal=False, process=None, _stdout=None, _stderr=None,
                               container_id=CID, container_cidfile=self.cidfile(CID),
                               stats_path=self.work / "missing.json")
        broker._record = lambda **values: recorded.update(values)
        self.docker({CID: {"status": "exited", "autoremove": False, "polls": 0}},
                    rm_refusal="Error response from daemon: device or resource busy")
        broker.close()
        self.assertIs(recorded["container_absent"], False)
        self.assertTrue(recorded["cleanup_errors"])

    def test_harbor_cleanup_awaits_removal(self):
        self.docker({CID: self.removing(3)})
        record = self.mod["harbor"].remove_owned_container("builder", CID)
        self.assertIs(record["absent_after_cleanup"], True, record)


if __name__ == "__main__":
    unittest.main()
