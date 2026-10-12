"""Reference control and negative controls for the 2026-09-20 Result hardening.

The reference rollout is a minimal but *real* correct coordinator written with
plain local Git: it admits every participant, runs each declared worker/test
command exactly once in an owned linked worktree, binds every parent ``160000``
entry to the exact child candidate, publishes every target, releases its own
admissions and worktrees, and reports the identities it produced.  It exists to
prove that every hardened oracle check is simultaneously satisfiable, i.e. that
the task stays legitimately solvable.  The negative controls then show that each
check really fails for the behaviour a partial product exhibits.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evaluator.case_runtime import CaseRuntime
from evaluator import result_score_caps

ACTIONS = ["inspect", "create", "run", "status", "recover", "rollback", "attest"]
ROOT = Path(__file__).resolve().parents[2]


def scenario_asset(case_id: str) -> dict:
    path = ROOT / "test_cases" / case_id / "assets/scenario.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def make_runtime(directory: Path, case_id: str) -> CaseRuntime:
    return CaseRuntime({"case_id": case_id, "scenario": "reference control",
                        "allowed_actions": ACTIONS, "task_input": "reference control task",
                        "scenario_asset": scenario_asset(case_id)},
                       directory / "case", directory / "private.json")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if done.returncode:
        raise RuntimeError(" ".join(args) + ": " + done.stderr[-400:])
    return done.stdout.strip()


def store_object(runtime: CaseRuntime, data: bytes) -> str:
    """Persist bytes in the published content-addressed store and name them."""
    digest = hashlib.sha256(data).hexdigest()
    path = runtime.state_dir / "objects" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "sha256:" + digest


def ledger(runtime: CaseRuntime, generation: int, digests: tuple[str, ...] = ()) -> dict:
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 3, "generation": generation, "events": generation,
               "prepare_digests": list(digests)}
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    (runtime.state_dir / "ledger.json").write_bytes(raw)
    return {"schema_version": 3, "generation": generation, "event_count": generation,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}


def admission_path(repo: Path, transaction: str) -> Path:
    return repo / ".git" / "aider" / "transactions" / (transaction + ".json")


def acquire(runtime: CaseRuntime, transaction: str) -> None:
    for repo in runtime.repo_paths.values():
        path = admission_path(repo, transaction)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 3, "transaction_id": transaction, "fence": 1}),
                        encoding="utf-8")


def release(runtime: CaseRuntime, transaction: str) -> None:
    for repo in runtime.repo_paths.values():
        admission_path(repo, transaction).unlink(missing_ok=True)


def run_declared_command(runtime: CaseRuntime, repository_id: str, subtask: str,
                         worktree: Path, kind: str, plan_id: str) -> None:
    environment = dict(os.environ)
    environment.update({"AIDER_PLAN_ID": plan_id, "AIDER_SUBTASK_ID": subtask,
                        "AIDER_REPOSITORY_ID": repository_id,
                        "AIDER_BASE_REPO": str(runtime.repo_paths[repository_id]),
                        "AIDER_WORKTREE": str(worktree),
                        "AIDER_COMMAND_LOG": str(runtime.root / "command_invocations.log")})
    if kind == "integration":
        environment.pop("AIDER_SUBTASK_ID")
    done = subprocess.run([sys.executable, str(runtime.root / "worker.py"),
                           "worker" if kind == "worker" else "test"],
                          cwd=worktree, env=environment, text=True, capture_output=True, check=False)
    if done.returncode:
        raise RuntimeError("declared command failed: " + done.stderr[-300:])


def prepare_and_publish(runtime: CaseRuntime, plan_id: str, *, leak: bool = False) -> dict:
    """Children before parents; every parent gitlink bound to its exact child."""
    order = [item["id"] for item in runtime.repo_specs if item["id"] != "root"] + ["root"]
    published: dict[str, str] = {}
    for repository_id in order:
        repo = runtime.repo_paths[repository_id]
        worktree = runtime.root / ("owned-worktree-" + repository_id)
        git(repo, "worktree", "add", "--detach", "-q", str(worktree), "HEAD")
        subtask = "edit-" + repository_id
        run_declared_command(runtime, repository_id, subtask, worktree, "worker", plan_id)
        run_declared_command(runtime, repository_id, subtask, worktree, "test", plan_id)
        tracked = "transaction.txt" if repository_id == "root" else "component.txt"
        git(worktree, "add", tracked)
        if repository_id == "root":
            for child in order[:-1]:
                git(worktree, "update-index", "--cacheinfo",
                    "160000,%s,components/%s" % (published[child], child))
        git(worktree, "commit", "-q", "-m", "reference %s transaction" % runtime.case_id)
        candidate = git(worktree, "rev-parse", "HEAD")
        run_declared_command(runtime, repository_id, subtask, worktree, "integration", plan_id)
        published[repository_id] = candidate
        store_object(runtime, git(repo, "cat-file", "commit", candidate).encode("utf-8"))
        if not leak:
            git(repo, "update-ref", "refs/heads/main", candidate)
        git(repo, "worktree", "remove", "--force", str(worktree))
        git(repo, "worktree", "prune")
    return published


def committed_response(runtime: CaseRuntime, plan_id: str, transaction: str,
                       published: dict[str, str], book: dict) -> dict:
    targets = [{"repository_id": row["repository_id"], "ref": row["ref"],
                "expected_oid": row["expected_oid"], "final_oid": published[row["repository_id"]]}
               for row in runtime.plan()["publication"]["targets"]]
    return {
        "schema_version": 3, "operation": "run", "plan_id": plan_id, "transaction_id": transaction,
        "state": "committed",
        "repositories": [{"id": key, "candidate_commit": value} for key, value in published.items()],
        "decision": {"state": "complete", "participant_order": list(published)},
        "publication": {"state": "committed", "targets": targets},
        "cleanup": {"state": "complete", "remaining_worktrees": [], "remaining_branches": [],
                    "admissions_released": True},
        "ledger": book, "errors": [],
    }


def reference_publication_rollout(runtime: CaseRuntime) -> dict:
    """inspect -> create -> run -> (recover) -> status -> attest, all effects once.

    Cases whose world injects a death before the decision reach publication in
    the recovery step, which is what their disclosed roll-forward rule requires.
    ``status`` deliberately writes nothing: it reuses the durable ledger bytes
    the decision already produced.
    """
    plan_id = "%s-%s" % (runtime.case_id, runtime.nonce[:8])
    transaction = "txn-" + runtime.nonce[:10]
    recovers = runtime.case_id in {"test_002", "test_003"}
    runtime.adapter_request("inspect")
    runtime.record("inspect", {"state": "observed"}, 0)
    runtime.adapter_request("create")
    acquire(runtime, transaction)
    book = ledger(runtime, 1)
    runtime.record("create", {"schema_version": 3, "plan_id": plan_id, "transaction_id": transaction,
                              "state": "created", "ledger": book,
                              "coordination": {"owner_id": "agent-owner", "lease_token": "reference-lease-token",
                                               "fence": 1}}, 0)
    runtime.adapter_request("run")
    if recovers:
        book = ledger(runtime, 2)
        runtime.record("run", {"schema_version": 3, "plan_id": plan_id, "transaction_id": transaction,
                               "state": "blocked", "reason": "process_group_killed_after_prepare",
                               "ledger": book, "errors": ["interrupted"]}, 0)
        runtime.adapter_request("recover")
    published = prepare_and_publish(runtime, plan_id)
    release(runtime, transaction)
    digests = tuple(store_object(runtime, git(runtime.repo_paths[key], "cat-file", "commit", value).encode("utf-8"))
                    for key, value in published.items())
    book = ledger(runtime, 3 if recovers else 2, digests)
    response = committed_response(runtime, plan_id, transaction, published, book)
    response["cleanup"]["state"] = "complete"
    runtime.record("recover" if recovers else "run", response, 0)
    runtime.adapter_request("status")
    runtime.record("status", {**response, "operation": "status"}, 0)
    runtime.record("attest", {"state": "observed"}, 0)
    return response


def reference_fail_closed_rollout(runtime: CaseRuntime) -> dict:
    plan_id = "%s-%s" % (runtime.case_id, runtime.nonce[:8])
    transaction = "txn-" + runtime.nonce[:10]
    runtime.adapter_request("inspect")
    runtime.record("inspect", {"state": "observed"}, 0)
    runtime.adapter_request("create")
    book = ledger(runtime, 1)
    runtime.record("create", {"schema_version": 3, "plan_id": plan_id, "transaction_id": transaction,
                              "state": "created", "ledger": book,
                              "coordination": {"owner_id": "agent-owner",
                                               "lease_token": "reference-lease-token", "fence": 1}}, 0)
    runtime.adapter_request("status")
    response = {
        "schema_version": 3, "operation": "status", "plan_id": plan_id, "transaction_id": transaction,
        "state": "blocked", "reason": "corrupt_global_decision",
        "repositories": [{"id": item["id"], "state": "blocked", "base_revision": runtime.base_oids[item["id"]],
                          "candidate_commit": None, "quarantine_digest": None}
                         for item in runtime.repo_specs],
        "decision": {"state": "aborted"},
        "publication": {"state": "pending", "targets": []},
        "cleanup": {"state": "preserved", "remaining_worktrees": [], "remaining_branches": [],
                    "admissions_released": True},
        "ledger": book, "errors": ["decision_digest mismatch"],
    }
    runtime.record("status", response, 0)
    runtime.record("attest", {"state": "observed"}, 0)
    return response


class ReferenceControl(unittest.TestCase):
    def test_reference_publication_satisfies_every_hardened_check(self):
        for case_id in ("test_001", "test_002", "test_003"):
            with self.subTest(case=case_id), tempfile.TemporaryDirectory() as directory:
                runtime = make_runtime(Path(directory), case_id)
                reference_publication_rollout(runtime)
                checks = runtime.semantic_comparison()["checks"]
                self.assertEqual(runtime.semantic_comparison()["expected_terminal_class"], "publication")
                failing = sorted(key for key, value in checks.items() if value is not True)
                # `fail_closed_evidenced` is the mutually exclusive alternative
                # terminal; a correct publication necessarily leaves it false.
                self.assertEqual([key for key in failing if key != "fail_closed_evidenced"],
                                 [], failing)
                for key in ("publication_independently_verified", "safe_terminal_or_refusal",
                            "durable_transaction_observed", "commands_executed_at_most_once",
                            "owned_resources_released", "read_only_status_left_no_durable_change",
                            "repeat_requests_repeated_no_effect", "predecision_quarantine_respected",
                            "foreign_state_preserved"):
                    self.assertIs(checks.get(key), True, "%s: %s" % (case_id, key))

    def test_reference_fail_closed_satisfies_guard_cases(self):
        for case_id in ("test_004", "test_006"):
            with self.subTest(case=case_id), tempfile.TemporaryDirectory() as directory:
                runtime = make_runtime(Path(directory), case_id)
                reference_fail_closed_rollout(runtime)
                comparison = runtime.semantic_comparison()
                self.assertEqual(comparison["expected_terminal_class"], "fail_closed")
                checks = comparison["checks"]
                for key in ("fail_closed_evidenced", "safe_terminal_or_refusal",
                            "foreign_state_preserved", "owned_resources_released",
                            "terminal_receipt_observed", "required_followup_observation",
                            "read_only_status_left_no_durable_change"):
                    self.assertIs(checks.get(key), True, "%s: %s" % (case_id, key))

    def test_reference_rollout_issues_no_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            response = reference_publication_rollout(runtime)
            contract = self.contract(Path(directory), runtime, response)
            violated = [item["cap_id"] for item in contract["entries"] if item["status"] == "violated"]
            self.assertEqual(violated, [], contract)
            self.assertIsNone(result_score_caps.effective_ceiling(contract))

    # ------------------------------------------------------------ negatives
    def test_blocked_everywhere_fails_a_publication_case(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            runtime.record("inspect", {"state": "observed"}, 0)
            book = ledger(runtime, 1)
            blocked = {"schema_version": 3, "plan_id": plan_id, "transaction_id": "txn-blocked",
                       "state": "blocked", "reason": "lease_expired", "ledger": book, "errors": []}
            runtime.record("create", {**blocked, "state": "created"}, 0)
            runtime.record("run", blocked, 0)
            runtime.record("recover", blocked, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["durable_transaction_observed"], True)
            self.assertIs(checks["fail_closed_evidenced"], True)
            # A justified-looking refusal is still the wrong terminal here.
            self.assertIs(checks["publication_independently_verified"], False)
            self.assertIs(checks["safe_terminal_or_refusal"], False)
            contract = self.contract(Path(directory), runtime, blocked)
            self.assertEqual(result_score_caps.effective_ceiling(contract), 25)

    def test_hollow_refusal_envelope_is_observed(self):
        """The 0920-fh-002 shape: settled `blocked` with `repositories: []`."""
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_006")
            plan_id = "test_006-%s" % runtime.nonce[:8]
            runtime.adapter_request("inspect")
            runtime.record("inspect", {"state": "observed"}, 0)
            for action in ("status", "recover"):
                runtime.adapter_request(action)
                runtime.record(action, {"state": "blocked", "plan_id": plan_id,
                                        "transaction_id": "rejected-" + action,
                                        "reason": "no transaction state exists at state_dir",
                                        "repositories": [],
                                        "ledger": {"schema_version": 3, "generation": 0,
                                                   "digest": "sha256:" + "e3b0c442" * 8}}, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["terminal_receipt_observed"], False)
            self.assertIs(checks["durable_transaction_observed"], False)
            self.assertIs(checks["fail_closed_evidenced"], False)
            self.assertIs(checks["safe_terminal_or_refusal"], False)
            contract = self.contract(Path(directory), runtime,
                                     runtime.responses["recover"])
            violated = sorted(item["cap_id"] for item in contract["entries"]
                              if item["status"] == "violated")
            # test_006's oracle expects a fail-closed refusal taken before any
            # durable transaction is formed, so c1 cannot be the condition that
            # decides this case; the hollow refusal is still bound by c2 and
            # c11, which read the refusal itself.
            self.assertEqual(violated, sorted(["c11_no_terminal_receipt_or_followup",
                                               "c2_case_terminal_not_evidenced"]), violated)
            durable = next(item for item in contract["entries"]
                           if item["cap_id"] == "c1_no_durable_transaction")
            self.assertEqual(durable["status"], "unavailable")
            self.assertIn("expected_terminal_class=fail_closed", durable["reason"])
            self.assertEqual(result_score_caps.effective_ceiling(contract), 25)

    def test_publication_case_keeps_the_durable_transaction_ceiling(self):
        """The c1 exemption is bound to the oracle's own terminal class."""
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            runtime.adapter_request("inspect")
            runtime.record("inspect", {"state": "observed"}, 0)
            for action in ("status", "recover"):
                runtime.adapter_request(action)
                runtime.record(action, {"state": "blocked", "plan_id": plan_id,
                                        "transaction_id": "rejected-" + action,
                                        "reason": "no transaction state exists at state_dir",
                                        "repositories": [],
                                        "ledger": {"schema_version": 3, "generation": 0,
                                                   "digest": "sha256:" + "e3b0c442" * 8}}, 0)
            comparison = runtime.semantic_comparison()
            self.assertEqual(comparison["expected_terminal_class"], "publication")
            self.assertIs(comparison["checks"]["durable_transaction_observed"], False)
            contract = self.contract(Path(directory), runtime, runtime.responses["recover"])
            durable = next(item for item in contract["entries"]
                           if item["cap_id"] == "c1_no_durable_transaction")
            self.assertEqual(durable["status"], "violated")
            self.assertEqual(result_score_caps.effective_ceiling(contract), 15)

    def test_impure_status_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_006")
            runtime.record("inspect", {"state": "observed"}, 0)
            book = ledger(runtime, 1)
            runtime.record("status", {"state": "blocked", "reason": "x", "plan_id": "p",
                                      "transaction_id": "t", "ledger": book}, 0)
            acquire(runtime, "txn-impure")
            runtime.record("status", {"state": "blocked", "reason": "x", "plan_id": "p",
                                      "transaction_id": "t", "ledger": book}, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["read_only_status_left_no_durable_change"], False)
            self.assertIs(checks["owned_resources_released"], False)

    def test_replayed_commands_are_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            runtime.record("inspect", {"state": "observed"}, 0)
            prepare_and_publish(runtime, plan_id)
            log = runtime.root / "command_invocations.log"
            log.write_text(log.read_text(encoding="utf-8") * 2, encoding="utf-8")
            book = ledger(runtime, 1)
            runtime.record("run", {"state": "blocked", "reason": "replayed", "plan_id": plan_id,
                                   "transaction_id": "t", "ledger": book}, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["commands_executed_at_most_once"], False)

    def test_predecision_object_leak_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            runtime.record("inspect", {"state": "observed"}, 0)
            published = prepare_and_publish(runtime, plan_id)
            book = ledger(runtime, 1)  # objects were stored by prepare_and_publish
            # The refs moved, but the response still reports the transaction as
            # undecided while the candidates are already in the ordinary ODB.
            runtime.record("create", {"state": "created", "plan_id": plan_id, "transaction_id": "t",
                                      "ledger": book,
                                      "repositories": [{"id": key, "candidate_commit": value}
                                                       for key, value in published.items()]}, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["predecision_quarantine_respected"], False)

    def test_foreign_admission_mutation_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_004")
            component = runtime.repo_paths["component"]
            (component / ".git/aider/transactions/admission.json").write_text("{\"stolen\": true}",
                                                                             encoding="utf-8")
            runtime.record("inspect", {"state": "observed"}, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["foreign_state_preserved"], False)

    def test_candidates_without_a_content_addressed_store_are_capped(self):
        """The 0920-hd-002 shape: a real publication with no state_dir/objects."""
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            transaction = "txn-" + runtime.nonce[:10]
            runtime.adapter_request("inspect")
            runtime.record("inspect", {"state": "observed"}, 0)
            runtime.adapter_request("create")
            acquire(runtime, transaction)
            book = ledger(runtime, 1)
            runtime.record("create", {"schema_version": 3, "plan_id": plan_id,
                                      "transaction_id": transaction, "state": "created",
                                      "ledger": book}, 0)
            runtime.adapter_request("run")
            published = prepare_and_publish(runtime, plan_id)
            for path in sorted((runtime.state_dir / "objects/sha256").iterdir()):
                path.unlink()
            (runtime.state_dir / "objects/sha256").rmdir()
            (runtime.state_dir / "objects").rmdir()
            release(runtime, transaction)
            book = ledger(runtime, 2)
            response = committed_response(runtime, plan_id, transaction, published, book)
            runtime.record("run", response, 0)
            checks = runtime.semantic_comparison()["checks"]
            self.assertIs(checks["publication_independently_verified"], True)
            self.assertIs(checks["safe_terminal_or_refusal"], True)
            self.assertIs(checks["content_addressed_object_store_valid"], False)
            contract = self.contract(Path(directory), runtime, response)
            self.assertEqual(result_score_caps.effective_ceiling(contract), 30)

    def test_report_without_observed_identities_is_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            response = reference_publication_rollout(runtime)
            contract = self.contract(Path(directory), runtime, response, artifact_text=json.dumps(
                {"schema_version": "agentswe-aider-agent-result/v1", "case_id": "test_001",
                 "observations": {"actions_executed": ["run"]}, "integrity": {},
                 "decision": {"completion_claim": True}, "safety": {}}))
            report = next(item for item in contract["entries"] if item["cap_id"] == "c9_report_not_evidence_bound")
            self.assertEqual(report["status"], "violated")
            self.assertEqual(result_score_caps.effective_ceiling(contract), 35)

    def test_contract_is_accepted_by_the_shared_judge_loader(self):
        judge = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
        if not judge.is_file():
            self.skipTest("shared Result judge is unavailable on this host")
        import importlib.util
        sys.path.insert(0, str(judge.parent))
        try:
            spec = importlib.util.spec_from_file_location("hardening_result_judge", str(judge))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest("shared Result judge is not importable here: %s" % exc)
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            runtime = make_runtime(Path(directory), "test_001")
            plan_id = "test_001-%s" % runtime.nonce[:8]
            runtime.record("inspect", {"state": "observed"}, 0)
            book = ledger(runtime, 1)
            blocked = {"state": "blocked", "reason": "lease_expired", "plan_id": plan_id,
                       "transaction_id": "t", "ledger": book, "errors": []}
            runtime.record("create", {**blocked, "state": "created"}, 0)
            runtime.record("run", blocked, 0)
            base = Path(directory)
            contract = self.contract(base, runtime, blocked)
            path = base / "caps.json"
            result_score_caps.write_contract(path, contract)
            inputs = {"rubric": base / "rubric.md", "native_evidence": base / "native.json",
                      "oracle_summary": base / "oracle.json"}
            cap, loaded = module.load_score_caps(path, "test_001", inputs)
            self.assertEqual(loaded["schema_version"], result_score_caps.SCHEMA_VERSION)
            self.assertEqual(cap, result_score_caps.effective_ceiling(contract))
            self.assertEqual(module.load_dimensions(
                module.rubric_dimensions_path(ROOT / "evaluator/rubric.md")),
                json.loads((ROOT / "evaluator/result_dimensions.json").read_text(encoding="utf-8")))

    # -------------------------------------------------------------- helpers
    def contract(self, base: Path, runtime: CaseRuntime, response: dict,
                 artifact_text: str | None = None) -> dict:
        comparison = runtime.semantic_comparison()
        state = json.loads((runtime.state_path).read_text(encoding="utf-8"))
        oracle = base / "oracle.json"
        oracle.write_text(json.dumps({"case_id": runtime.case_id, "semantic_comparison": comparison},
                                     ensure_ascii=False), encoding="utf-8")
        native = base / "native.json"
        native.write_text(json.dumps({"case_id": runtime.case_id, "state": state}, ensure_ascii=False),
                          encoding="utf-8")
        rubric = base / "rubric.md"
        rubric.write_text("reference rubric bytes\n", encoding="utf-8")
        artifact = base / "agent_artifact.json"
        if artifact_text is None:
            identities = sorted(result_score_caps.observed_identities(oracle, native))
            artifact_text = json.dumps({"schema_version": "agentswe-aider-agent-result/v1",
                                        "case_id": runtime.case_id, "observed_identities": identities,
                                        "observations": {}, "integrity": {}, "decision": {},
                                        "safety": {}})
        artifact.write_text(artifact_text, encoding="utf-8")
        return result_score_caps.build_contract(runtime.case_id, rubric=rubric, native_evidence=native,
                                                oracle_summary=oracle, artifact=artifact)


if __name__ == "__main__":
    unittest.main()
