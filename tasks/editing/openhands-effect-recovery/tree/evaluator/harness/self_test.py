#!/usr/bin/env python3
"""Dependency-free evaluator integrity, isolation, and scorer controls."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import (
    ABSOLUTE_LIMITS,
    ASSERTION_WEIGHTS,
    CASE_IDS,
    CRITICAL_LIMITS,
    PROCESS_TREE_MEMORY_LIMIT_BYTES,
    ROOT,
    HarnessError,
    aggregate_case_results,
    apply_patch_once,
    changed_paths,
    parse_vitest,
    redact,
)


def check(condition: bool, label: str) -> dict[str, object]:
    if not condition:
        raise AssertionError(label)
    return {"id": label, "passed": True}


def audit_package(root: Path) -> None:
    builder_inputs = sorted((root / "input").glob("0[1-4]_*.md"))
    if len(builder_inputs) != 4:
        raise AssertionError("builder input inventory is not exactly four")
    public_cases = sorted(
        path.name for path in (root / "dev_cases").glob("dev_*") if path.is_dir()
    )
    if public_cases != ["dev_001", "dev_002"]:
        raise AssertionError("public case inventory mismatch")
    for case_id in public_cases:
        case = root / "dev_cases" / case_id
        if not (case / "input.md").is_file() or not list(case.glob("public.test.ts*")):
            raise AssertionError(f"public case incomplete: {case_id}")

    hardening_markers = {
        "test_001": (
            "workspaceConflictSafePlan", "workspaceOrderedCommit",
            "workspaceDelayedResponseFence", "workspaceProjectionIsolation",
        ),
        "test_002": (
            "workspaceChunkResponseLoss", "workspaceCrashBoundaries",
            "workspaceCommitResponseLoss", "workspaceManifestAndChunkValidation",
        ),
        "test_003": ("syncTermIsOrthogonal", "workspaceFenceIsOrthogonal"),
        "test_004": ("syncProjectionIsolation", "workspaceProjectionIsolation"),
        "test_005": (
            "migratedCompactionScopeIsolation",
            "removeFailureAfterPublication",
            "dispatcherMigratedCursorHandoff",
            "syncRecordsSurviveLedgerCompaction",
            "workspaceRecordsSurviveLedgerCompaction",
        ),
        "test_006": (
            "lateRejectedCallbackAfterTakeover",
            "replacementWatchABA",
            "replayUsesDistinctUiEventId",
            "dispatcherProductionHandoff",
            "terminalEchoSuppression",
            "productionSyncLateResponseFence",
            "productionSyncNoticeIsolation",
            "productionSyncResponseLossRetry",
            "productionWorkspaceCommitRecovery",
            "productionWorkspaceTakeoverFence",
        ),
    }
    observed_cases: set[str] = set()
    for case_id in CASE_IDS:
        input_path = root / "test_cases" / case_id / "input.md"
        fixture = root / "test_cases" / case_id / "assets" / "fixtures.json"
        candidates = list((root / "evaluator" / "tests").glob(f"{case_id}.test.ts*"))
        if not input_path.is_file() or not fixture.is_file() or len(candidates) != 1:
            raise AssertionError(f"hidden case incomplete: {case_id}")
        source = candidates[0].read_text(encoding="utf-8")
        markers = [
            (match.group(1), int(match.group(2)))
            for match in re.finditer(r"\[(OH\d{3}):(\d+)\]", source)
        ]
        found = dict(markers)
        if len(found) != len(markers):
            raise AssertionError(f"duplicate semantic assertion ID: {case_id}")
        if found != ASSERTION_WEIGHTS[case_id] or sum(found.values()) != 100:
            raise AssertionError(f"semantic assertion manifest mismatch: {case_id}")
        markers_for_case = hardening_markers.get(case_id, ())
        if any(marker not in source for marker in markers_for_case):
            raise AssertionError(f"cycle-8 hardening path missing: {case_id}")
        observed_cases.add(case_id)
    if observed_cases != set(CASE_IDS):
        raise AssertionError("hidden case manifest mismatch")

    public_surface = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "dev_cases").rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".ts", ".tsx"}
    )
    for marker in (
        "callbackRejectionRecovery",
        "replacementWatchABA",
        "replayUsesDistinctUiEventId",
        "dispatcherReconnectHandoff",
        "createRecoveryEventDispatcher",
        "syncResponseLossRetry",
        "syncFrozenTabTakeover",
        "syncSameTabReincarnation",
        "workspaceChunkResume",
        "workspaceConflictBlocksAll",
        "workspaceCommitReconciliation",
    ):
        if marker not in public_surface:
            raise AssertionError(f"public cycle-8 contract path missing: {marker}")

    public_files = [
        path for path in (root / "dev_cases").rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".md", ".json"}
    ]
    for path in public_files:
        text = path.read_text(encoding="utf-8")
        if re.search(
            r"(?:^|[/'\"])(?:evaluator|test_cases)/|\b(?:from|import)\s+(?:evaluator|test_cases)(?:[./\s])",
            text,
            re.M,
        ):
            raise AssertionError(f"public runner leakage: {path.relative_to(root)}")

    runner = (root / "dev_cases" / "run_dev_case.py").read_text(encoding="utf-8")
    common = (root / "evaluator" / "harness" / "common.py").read_text(encoding="utf-8")
    suite = (root / "evaluator" / "harness" / "evaluate_suite.py").read_text(encoding="utf-8")
    type_probe = (root / "dev_cases" / "recovery-contract.type-test.ts").read_text()
    if runner.index("recovery-contract.type-test.ts") > runner.index("typecheck = run_typecheck_gate"):
        raise AssertionError("public contract probe is injected after typecheck")
    if suite.index("inject_public_contract_probe(worktree)") > suite.index("typecheck = run_typecheck_gate"):
        raise AssertionError("formal contract probe is injected after typecheck")
    if (
        'case_id == "test_006"' not in common
        or 'destination / "sync-helpers.ts"' not in common
    ):
        raise AssertionError("sync helper is not injected only for its behavioral case")
    if (
        'case_id in {"test_001", "test_002", "test_006"}' not in common
        or 'destination / "workspace-helpers.ts"' not in common
    ):
        raise AssertionError("workspace helper is not isolated to workspace cases")
    for required in (
        "crashAfter", "issueInspectionGrant", "inspectRecoveryAuthorized",
        "executeRecoverableEffect", "watch(authorized)", "recoveryStorageKey",
        "ingestRecoveryEvent",
        "createConversationEventDispatcher",
        "createRecoveryEventDispatcher",
        "openSession",
        "sinceEventCursor",
    ):
        if required not in type_probe:
            raise AssertionError(f"type probe misses v3 contract: {required}")
    if "recovery-sync-coordinator" in type_probe or "RecoverySync" in type_probe:
        raise AssertionError("new sync surface was turned into a global validity probe")
    if "workspace-recovery-reconciler" in type_probe or "WorkspaceRecovery" in type_probe:
        raise AssertionError("workspace surface was turned into a global validity probe")
    for case_id in ("test_003", "test_004", "test_005"):
        source = next((root / "evaluator/tests").glob(f"{case_id}.test.ts*"))
        text = source.read_text(encoding="utf-8")
        if "recovery-sync-coordinator" in text or "./sync-helpers" in text:
            raise AssertionError(f"independent legacy case imports sync product: {case_id}")
        if "workspace-recovery-reconciler" in text or "./workspace-helpers" in text:
            raise AssertionError(f"independent legacy case imports workspace product: {case_id}")
    if (
        "aggregation refused" not in common
        or "aggregate_case_results(summary" not in suite
        or '"evaluator_error"' not in suite
    ):
        raise AssertionError("suite can silently aggregate missing/evaluator-error cases")

    combined_harness = runner + common
    forbidden_memory = ("RLIMIT_AS", "ulimit -v", "--jitless")
    if any(item in combined_harness for item in forbidden_memory):
        raise AssertionError("forbidden memory mechanism present")
    if "smaps_rollup" not in runner or "smaps_rollup" not in common:
        raise AssertionError("actual PSS measurement missing from a runner")
    if PROCESS_TREE_MEMORY_LIMIT_BYTES != 8 * 1024 * 1024 * 1024:
        raise AssertionError("resource ceiling drift")
    public_limit = re.search(r"PROCESS_TREE_MEMORY_LIMIT_BYTES\s*=\s*([^\n]+)", runner)
    if not public_limit or public_limit.group(1).strip() != "8 * 1024 * 1024 * 1024":
        raise AssertionError("public/formal memory semantics differ")

    schema = json.loads((root / "input" / "recovery-checkpoint.schema.json").read_text())
    if schema["properties"]["schemaVersion"].get("const") != 3:
        raise AssertionError("canonical schema is not v3")
    if set(CRITICAL_LIMITS) != set(CASE_IDS):
        raise AssertionError("every case must have a defining cross-instance local limit")
    if any(CRITICAL_LIMITS[case_id][1] != 30 for case_id in ("test_001", "test_002", "test_006")):
        raise AssertionError("workspace-usability limits must remain uniformly calibrated to 30")
    if CRITICAL_LIMITS["test_005"][0] != ("OH401", "OH402", "OH404"):
        raise AssertionError("migration/compaction critical limit missing")
    if CRITICAL_LIMITS["test_006"][0] != ("OH507", "OH508"):
        raise AssertionError("production workspace critical limit missing")
    if ABSOLUTE_LIMITS.get("test_004", (None,))[0] != "OH303":
        raise AssertionError("redaction absolute limit missing")


def expect_audit_failure(root: Path) -> bool:
    try:
        audit_package(root)
    except (AssertionError, ValueError):
        return True
    return False


def copy_audit_surface(destination: Path) -> None:
    (destination / "input").mkdir(parents=True)
    for path in ROOT.glob("input/0[1-4]_*.md"):
        shutil.copy2(path, destination / "input" / path.name)
    shutil.copy2(
        ROOT / "input/recovery-checkpoint.schema.json",
        destination / "input/recovery-checkpoint.schema.json",
    )
    for relative in ("dev_cases", "test_cases", "evaluator/tests", "evaluator/harness"):
        shutil.copytree(ROOT / relative, destination / relative)


def fake_command(exit_code: int = 1):
    return type("Result", (), {
        "exit_code": exit_code,
        "evidence": lambda self: {"exit_code": exit_code},
    })()


def main() -> int:
    assertions = []
    audit_package(ROOT)
    assertions.append(check(True, "SELF_PACKAGE_INVENTORY_AND_CONTRACT"))

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "selftest@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Evaluator Self Test"], cwd=repo, check=True)
        target = repo / "src/api/recovery/recovery-evaluator-adapter.ts"
        target.parent.mkdir(parents=True)
        target.write_text("baseline\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
        target.write_text("positive evaluator fixture\n")
        patch = root / "solution.patch"
        patch.write_text(subprocess.run(
            ["git", "diff"], cwd=repo, text=True, capture_output=True, check=True,
        ).stdout)
        subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)
        apply_patch_once(repo, patch)
        assertions.append(check(
            target.read_text() == "positive evaluator fixture\n",
            "SELF_PATCH_APPLIES_EXACTLY_ONCE",
        ))
        try:
            changed_paths(root / "missing.patch")
            missing = False
        except HarnessError:
            missing = True
        assertions.append(check(missing, "SELF_MISSING_ENTRY_REJECTED"))

        report = root / "vitest.json"
        report.write_text(json.dumps({"testResults": [{"assertionResults": [
            {"fullName": "[OH001:10] basic fixture", "status": "passed"},
            {"fullName": "[OH002:40] takeover negative control", "status": "failed",
             "failureMessages": ["two owners won"]},
            {"fullName": "[OH003:40] stale settlement fixture", "status": "passed"},
            {"fullName": "[OH004:10] stale event fixture", "status": "passed"},
        ]}]}))
        scored = parse_vitest("test_001", report, fake_command())
        assertions.append(check(
            scored["raw_assertion_score"] == 60 and
            scored["score"] == 30 and
            scored["local_limit"]["maximum"] == 30,
            "SELF_CROSS_INSTANCE_CAP_ACCOUNTING",
        ))

        redaction_report = root / "redaction.json"
        redaction_report.write_text(json.dumps({"testResults": [{"assertionResults": [
            {"fullName": "[OH301:30] authority", "status": "passed"},
            {"fullName": "[OH302:25] lifecycle", "status": "passed"},
            {"fullName": "[OH303:30] redaction", "status": "failed",
             "failureMessages": ["AKIA_RECOVERY_SELFTEST"]},
            {"fullName": "[OH304:15] summary", "status": "passed"},
        ]}]}))
        redaction = parse_vitest("test_004", redaction_report, fake_command())
        assertions.append(check(
            redaction["raw_assertion_score"] == 70 and redaction["score"] == 0,
            "SELF_REDACTION_ABSOLUTE_CAP",
        ))
        assertions.append(check(
            "AKIA" not in redact("AKIA_RECOVERY_SELFTEST_92Z"),
            "SELF_EVIDENCE_REDACTION",
        ))

        missing_report = root / "does-not-exist.json"
        behavioral_zero = parse_vitest("test_002", missing_report, fake_command(124))
        assertions.append(check(
            behavioral_zero["score"] == 0 and
            behavioral_zero["case_status"] == "behavioral_failure" and
            len(behavioral_zero["assertions"]) == 4,
            "SELF_CASE_TIMEOUT_IS_EXPLICIT_ZERO",
        ))
        complete_results = {
            case_id: {
                "case_id": case_id,
                "case_status": "behavioral_failure" if case_id == "test_002" else "completed",
                "score": 0 if case_id == "test_002" else 10,
            }
            for case_id in CASE_IDS
        }
        aggregate = aggregate_case_results(complete_results)
        assertions.append(check(
            aggregate["aggregation_valid"] and aggregate["case_scores"]["test_002"] == 0,
            "SELF_BEHAVIORAL_ZERO_AGGREGATES",
        ))
        try:
            aggregate_case_results({key: value for key, value in complete_results.items() if key != "test_006"})
            missing_rejected = False
        except HarnessError:
            missing_rejected = True
        assertions.append(check(missing_rejected, "SELF_MISSING_RESULT_REFUSES_AGGREGATION"))
        errored_results = {key: dict(value) for key, value in complete_results.items()}
        errored_results["test_006"] = {"case_id": "test_006", "case_status": "evaluator_error"}
        try:
            aggregate_case_results(errored_results)
            evaluator_error_rejected = False
        except HarnessError:
            evaluator_error_rejected = True
        assertions.append(check(
            evaluator_error_rejected, "SELF_EVALUATOR_ERROR_REFUSES_AGGREGATION",
        ))
        first = parse_vitest("test_001", report, fake_command())
        second = parse_vitest("test_001", report, fake_command())
        first["assertions"][0]["earned"] = 0
        assertions.append(check(
            second["assertions"][0]["earned"] == 10,
            "SELF_CASE_SCORING_INDEPENDENCE",
        ))

        mutation = root / "mutation"
        copy_audit_surface(mutation)
        weakened = mutation / "evaluator/tests/test_001.test.ts"
        weakened.write_text(weakened.read_text().replace("[OH003:40]", "[OH003:39]"))
        assertions.append(check(
            expect_audit_failure(mutation), "SELF_ASSERTION_WEAKENING_REJECTED",
        ))

        duplicate = root / "duplicate"
        copy_audit_surface(duplicate)
        duplicated = duplicate / "evaluator/tests/test_001.test.ts"
        duplicated.write_text(duplicated.read_text() + '\nit("[OH001:10] duplicate", () => {});\n')
        assertions.append(check(
            expect_audit_failure(duplicate), "SELF_DUPLICATE_ASSERTION_REJECTED",
        ))

        omission = root / "omission"
        copy_audit_surface(omission)
        (omission / "test_cases/test_004/input.md").unlink()
        assertions.append(check(
            expect_audit_failure(omission), "SELF_HIDDEN_OMISSION_REJECTED",
        ))

        leakage = root / "leakage"
        copy_audit_surface(leakage)
        leak_runner = leakage / "dev_cases/run_dev_case.py"
        leak_runner.write_text(
            leak_runner.read_text() + "\nfrom evaluator.harness.common import run_case\n"
        )
        assertions.append(check(
            expect_audit_failure(leakage), "SELF_PUBLIC_LEAKAGE_REJECTED",
        ))

        resource_drift = root / "resource-drift"
        copy_audit_surface(resource_drift)
        drift_runner = resource_drift / "dev_cases/run_dev_case.py"
        drift_runner.write_text(drift_runner.read_text().replace(
            "8 * 1024 * 1024 * 1024", "2 * 1024 * 1024 * 1024", 1,
        ))
        assertions.append(check(
            expect_audit_failure(resource_drift), "SELF_RESOURCE_DRIFT_REJECTED",
        ))

    print(json.dumps({"valid": True, "assertions": assertions}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
