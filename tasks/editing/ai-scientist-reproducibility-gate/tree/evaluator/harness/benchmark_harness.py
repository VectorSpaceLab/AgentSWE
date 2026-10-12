from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
from typing import Any


BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = BENCHMARK_ROOT / "dev_cases"
sys.path.insert(0, str(PUBLIC_DIR))

from public_harness import (  # noqa: E402
    HarnessError,
    RECEIPT_NAMES,
    RELEASE_OUTPUTS,
    SCIENTIFIC_OUTPUTS,
    budget_check,
    expected_charge,
    auxiliary_checks,
    file_sha256,
    find_key,
    inspect_capsule,
    inspect_provenance,
    integration_score,
    load_context,
    lower_corpus,
    ordinary_probe,
    prepare_submission,
    read_json,
    release_outputs,
    provenance_retry_stable,
    run_transaction,
    science_checks,
    science_gate_check,
    scientific_claim_contract,
    scientific_evidence_attestation,
    structured_error,
    tree_digest,
)
from oracles import SPECS  # noqa: E402


def load_manifest(case_id: str) -> dict[str, Any]:
    manifest = read_json(BENCHMARK_ROOT / "evaluator" / "manifests" / f"{case_id}.json")
    if not isinstance(manifest, dict) or manifest.get("case_id") != case_id or not isinstance(manifest.get("assertions"), dict):
        raise HarnessError(f"invalid assertion manifest for {case_id}")
    if any(not isinstance(value, int) or value < 0 for value in manifest["assertions"].values()) or sum(manifest["assertions"].values()) != 100:
        raise HarnessError(f"assertion manifest for {case_id} does not total 100")
    return manifest


def award(assertions: list[dict[str, Any]], weights: dict[str, int], assertion_id: str, passed: bool, evidence: str) -> None:
    if assertion_id not in weights:
        raise HarnessError(f"runtime assertion {assertion_id} is absent from manifest")
    points = weights[assertion_id]
    assertions.append({"id": assertion_id, "earned": points if passed else 0, "possible": points, "passed": bool(passed), "evidence": evidence})


def context_copy(context: dict[str, Any], **changes: Any) -> dict[str, Any]:
    copied = dict(context)
    copied.update(changes)
    return copied


def stage_capsule(session_store: Path, receipt: Any) -> Path | None:
    stage_path = find_key(receipt, {"stage_path"})
    if not isinstance(stage_path, str):
        return None
    relative = Path(stage_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return session_store / relative / "reproducibility_capsule.zip"


def stable_ids(first_tx: Any, first_budget: Any, second_tx: Any, second_budget: Any) -> bool:
    return (
        bool(find_key(first_tx, {"commit_id"}))
        and find_key(first_tx, {"commit_id"}) == find_key(second_tx, {"commit_id"})
        and bool(find_key(first_tx, {"capsule_id"}))
        and find_key(first_tx, {"capsule_id"}) == find_key(second_tx, {"capsule_id"})
        and bool(find_key(first_budget, {"settlement_id"}))
        and find_key(first_budget, {"settlement_id"}) == find_key(second_budget, {"settlement_id"})
    )


def _case1(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    ledger, sessions, budgets = root / "ledger.json", root / "sessions", root / "budgets"
    prepare = run_transaction(repo, workspace, root / "prepare", ledger, sessions, budgets, context, "prepare", timeout=timeout)
    status_pre = run_transaction(repo, workspace, root / "status-pre", ledger, sessions, budgets, context, "status", timeout=timeout)
    commit = run_transaction(repo, workspace, root / "commit", ledger, sessions, budgets, context, "commit", surface="launcher", timeout=timeout)
    committed_hashes = dict(commit["artifact_hashes"])
    committed_tx, committed_budget = commit.get(RECEIPT_NAMES[0]), commit.get(RECEIPT_NAMES[1])
    shutil.rmtree(root / "commit")
    retry = run_transaction(repo, workspace, root / "retry", ledger, sessions, budgets, context, "verify", timeout=timeout)
    status_post = run_transaction(repo, workspace, root / "status-post", ledger, sessions, budgets, context, "status", timeout=timeout)
    runs = [prepare, status_pre, commit, retry, status_post]
    weights = load_manifest("test_001")["assertions"]
    assertions: list[dict[str, Any]] = []
    charge = expected_charge(workspace, context)
    staged_path = stage_capsule(sessions, prepare.get(RECEIPT_NAMES[0]))
    staged = inspect_capsule(staged_path, workspace, context) if staged_path else {"valid": False, "structure": False, "manifest": False, "evidence": "no safe staged capsule path"}
    final_capsule = inspect_capsule(root / "retry" / "reproducibility_capsule.zip", workspace, context)
    reserved, reserved_evidence = budget_check(status_pre.get(RECEIPT_NAMES[1]), context, charge, "reserved", charge, 0)
    settled, settled_evidence = budget_check(status_post.get(RECEIPT_NAMES[1]), context, charge, "settled", 0, charge)
    ids_stable = stable_ids(committed_tx, committed_budget, retry.get(RECEIPT_NAMES[0]), retry.get(RECEIPT_NAMES[1]))
    bytes_stable = committed_hashes == retry["artifact_hashes"] and set(committed_hashes) == set(RELEASE_OUTPUTS)
    award(assertions, weights, "CAPSULE.EXACT_ARCHIVE", final_capsule["structure"], final_capsule["evidence"])
    award(assertions, weights, "CAPSULE.MANIFEST_PROVENANCE", final_capsule["manifest"], final_capsule["evidence"])
    award(assertions, weights, "CAPSULE.RESTART_BYTE_STABILITY", staged.get("valid", False) and bytes_stable, f"staged valid={staged.get('valid')}, committed/retry bytes stable={bytes_stable}")
    award(assertions, weights, "BUDGET.RESERVE_THEN_SETTLE", reserved and settled, f"pre {reserved_evidence}; post {settled_evidence}")
    award(assertions, weights, "BUDGET.EXACT_TOTALS", settled, f"computed charge={charge}; {settled_evidence}")
    award(assertions, weights, "BUDGET.SINGLE_SETTLEMENT", ids_stable and settled, f"stable settlement and commit IDs={ids_stable}; {settled_evidence}")
    award(assertions, weights, "CROSS.LOST_RESPONSE_REPAIR", bytes_stable and ids_stable, f"byte stable={bytes_stable}, IDs stable={ids_stable}")
    award(assertions, weights, "CROSS.ATOMIC_RELEASE_CHARGE", final_capsule["valid"] and settled and set(retry["artifact_hashes"]) == set(RELEASE_OUTPUTS), "complete verified release and one settled charge coexist")
    return assertions, runs, root / "retry", context


def _case2(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    sessions, budgets = root / "sessions", root / "budgets"
    first = context_copy(context, verification_id="race-release-one", request_id="race-one", owner_id="worker-one")
    second = context_copy(context, verification_id="race-release-two", request_id="race-two", owner_id="worker-two")

    def prepare_one(name: str, ctx: dict[str, Any]) -> dict[str, Any]:
        return run_transaction(repo, workspace, root / f"prepare-{name}", root / f"ledger-{name}.json", sessions, budgets, ctx, "prepare", timeout=timeout)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(prepare_one, "one", first)
        future_b = executor.submit(prepare_one, "two", second)
        prep_a, prep_b = future_a.result(), future_b.result()
    successful = [("one", first, prep_a), ("two", second, prep_b)]
    winners = [item for item in successful if item[2]["returncode"] == 0]
    losers = [item for item in successful if item[2]["returncode"] != 0]
    commit: dict[str, Any] = {"returncode": -999, "artifact_hashes": {}, "runtime_seconds": 0, "peak_memory_bytes": 0, "operation": "commit-not-run"}
    status = commit
    winner_capsule = {"valid": False, "evidence": "no established winner"}
    settled = False
    settled_evidence = "no established winner"
    winner_context = context
    if len(winners) == 1:
        name, winner_context, _ = winners[0]
        commit = run_transaction(repo, workspace, root / "winner-commit", root / f"ledger-{name}.json", sessions, budgets, winner_context, "commit", surface="launcher", timeout=timeout)
        status = run_transaction(repo, workspace, root / "winner-status", root / f"ledger-{name}.json", sessions, budgets, winner_context, "status", timeout=timeout)
        winner_capsule = inspect_capsule(root / "winner-commit" / "reproducibility_capsule.zip", workspace, winner_context)
        charge = expected_charge(workspace, winner_context)
        settled, settled_evidence = budget_check(status.get(RECEIPT_NAMES[1]), winner_context, charge, "settled", 0, charge)
    loser_error = (False, "no single loser")
    if len(losers) == 1:
        loser_error = structured_error(losers[0][2], (("budget", "limit", "available"), ("exhaust", "over", "insufficient", "reserve")))
    weights = load_manifest("test_002")["assertions"]
    assertions: list[dict[str, Any]] = []
    exact_race = len(winners) == len(losers) == 1
    loser_out = root / f"prepare-{losers[0][0]}" if losers else root / "no-loser"
    loser_clean = not any((loser_out / name).exists() for name in RELEASE_OUTPUTS + RECEIPT_NAMES)
    award(assertions, weights, "CAPSULE.WINNER_VALID", exact_race and winner_capsule.get("valid", False), winner_capsule.get("evidence", ""))
    award(assertions, weights, "CAPSULE.LOSER_NONE", exact_race and loser_clean, f"loser published no capsule/release/receipt={loser_clean}")
    award(assertions, weights, "BUDGET.CONCURRENT_SINGLE_RESERVATION", exact_race, f"successful prepares={len(winners)}, rejected prepares={len(losers)}")
    award(assertions, weights, "BUDGET.OVER_LIMIT_REJECTION", exact_race and loser_error[0], loser_error[1])
    award(assertions, weights, "CROSS.ESTABLISHED_WINNER", exact_race and commit.get("returncode") == 0 and settled, f"winner commit rc={commit.get('returncode')}; {settled_evidence}")
    award(assertions, weights, "CROSS.SHARED_STATE_INTEGRITY", exact_race and settled and winner_capsule.get("valid", False), "shared project state has one complete release and one settlement")
    runs = [prep_a, prep_b, commit, status]
    return assertions, runs, root / "winner-commit", winner_context


def _case3(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    ledger, sessions, budgets = root / "ledger.json", root / "sessions", root / "budgets"
    prepare = run_transaction(repo, workspace, root / "prepare", ledger, sessions, budgets, context, "prepare", timeout=timeout)
    fatal_context = context_copy(context, request_id="fatal-probe", run_manifest="run_manifest_fatal.json")
    fatal = run_transaction(repo, workspace, root / "fatal", root / "fatal-ledger.json", root / "fatal-sessions", root / "fatal-budgets", fatal_context, "prepare", timeout=timeout)
    staged_path = stage_capsule(sessions, prepare.get(RECEIPT_NAMES[0]))
    staged = inspect_capsule(staged_path, workspace, context) if staged_path else {"valid": False, "evidence": "no safe stage path"}
    if staged_path and staged_path.is_file():
        with staged_path.open("ab") as stream:
            stream.write(b"corrupt-evaluator-byte")
    failed = run_transaction(repo, workspace, root / "failed-commit", ledger, sessions, budgets, context, "commit", timeout=timeout)
    old_status = run_transaction(repo, workspace, root / "old-status", ledger, sessions, budgets, context, "status", timeout=timeout)
    recovered_context = context_copy(context, request_id="request-t2", owner_id="worker-new", generation=2)
    recovered = run_transaction(repo, workspace, root / "recovered", ledger, sessions, budgets, recovered_context, "verify", surface="launcher", takeover_generation=1, timeout=timeout)
    final_status = run_transaction(repo, workspace, root / "final-status", ledger, sessions, budgets, recovered_context, "status", timeout=timeout)
    runs = [prepare, fatal, failed, old_status, recovered, final_status]
    charge = expected_charge(workspace, context)
    reserved, reserved_evidence = budget_check(old_status.get(RECEIPT_NAMES[1]), context, charge, "reserved", charge, 0)
    settled, settled_evidence = budget_check(final_status.get(RECEIPT_NAMES[1]), recovered_context, charge, "settled", 0, charge)
    rejected, rejected_evidence = structured_error(failed, (("capsule", "stage", "artifact"), ("corrupt", "hash", "digest", "integrity")))
    final_capsule = inspect_capsule(root / "recovered" / "reproducibility_capsule.zip", workspace, recovered_context)
    failed_clean = not any((root / "failed-commit" / name).exists() for name in RELEASE_OUTPUTS)
    weights = load_manifest("test_003")["assertions"]
    assertions: list[dict[str, Any]] = []
    award(assertions, weights, "CAPSULE.STAGED_VALID", staged.get("valid", False), staged.get("evidence", ""))
    award(assertions, weights, "CAPSULE.CORRUPTION_REJECTED", rejected, rejected_evidence)
    award(assertions, weights, "CAPSULE.RECOVERED_VALID", final_capsule["valid"], final_capsule["evidence"])
    award(assertions, weights, "BUDGET.RESERVATION_PRESERVED", reserved, reserved_evidence)
    award(assertions, weights, "BUDGET.RECOVERY_SINGLE_SETTLEMENT", settled, settled_evidence)
    award(assertions, weights, "CROSS.FAILED_COMMIT_ISOLATION", rejected and failed_clean and reserved, f"failed release absent={failed_clean}, reservation preserved={reserved}")
    award(assertions, weights, "CROSS.GENERATION_RECOVERY", recovered["returncode"] == 0 and final_capsule["valid"] and settled, "higher generation produced one valid release and one settlement")
    fatal_error = structured_error(fatal, (("worker", "execution", "fatal"), ("pool_broken", "fatal", "blocked")))
    fatal_clean = not any((root / "fatal" / name).exists() for name in RELEASE_OUTPUTS + RECEIPT_NAMES)
    if "PROVENANCE.FATAL_FAILURE_REJECTED" in weights:
        award(assertions, weights, "PROVENANCE.FATAL_FAILURE_REJECTED", fatal_error[0] and fatal_clean, f"fatal run rejected without release or receipt={fatal_error[0] and fatal_clean}; {fatal_error[1]}")
    return assertions, runs, root / "recovered", recovered_context


def _case4(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    sessions, budgets, ledger = root / "sessions", root / "budgets", root / "ledger-old.json"
    prepare = run_transaction(repo, workspace, root / "prepare-v1", ledger, sessions, budgets, context, "prepare", timeout=timeout)
    staged_path = stage_capsule(sessions, prepare.get(RECEIPT_NAMES[0]))
    staged = inspect_capsule(staged_path, workspace, context) if staged_path else {"valid": False, "evidence": "no staged capsule"}
    drift_context = context_copy(context, run_manifest="run_manifest_v2.json", capsule_policy="capsule_policy_v2.json", budget_policy="budget_policy_v2.json")
    drift = run_transaction(repo, workspace, root / "drift-commit", ledger, sessions, budgets, drift_context, "commit", timeout=timeout)
    original_usage = (workspace / context["usage_statement"]).read_bytes()
    conflict_usage = json.loads(original_usage.decode())
    conflict_usage["calls"].append({**conflict_usage["calls"][0], "prompt_tokens": conflict_usage["calls"][0]["prompt_tokens"] + 1})
    (workspace / "usage_conflict.json").write_text(json.dumps(conflict_usage), encoding="utf-8")
    conflict_context = context_copy(context, request_id="usage-conflict", usage_statement="usage_conflict.json")
    usage_conflict = run_transaction(repo, workspace, root / "usage-conflict", root / "ledger-conflict.json", sessions, budgets, conflict_context, "prepare", timeout=timeout)
    (workspace / "usage_conflict.json").unlink()
    cancel = run_transaction(repo, workspace, root / "cancel-v1", ledger, sessions, budgets, context, "cancel", timeout=timeout)
    stale = run_transaction(repo, workspace, root / "stale-commit", ledger, sessions, budgets, context, "commit", timeout=timeout)
    fresh = context_copy(drift_context, verification_id="release-u4-new", request_id="policy-new", generation=1)
    commit = run_transaction(repo, workspace, root / "commit-v2", root / "ledger-new.json", sessions, budgets, fresh, "verify", surface="launcher", timeout=timeout)
    status = run_transaction(repo, workspace, root / "status-v2", root / "ledger-new.json", sessions, budgets, fresh, "status", timeout=timeout)
    runs = [prepare, drift, usage_conflict, cancel, stale, commit, status]
    drift_error = structured_error(drift, (("policy", "fingerprint", "request"), ("drift", "changed", "conflict", "mismatch")))
    usage_error = structured_error(usage_conflict, (("duplicate", "call"), ("conflict", "different", "content")))
    stale_error = structured_error(stale, (("cancel", "state", "phase"), ("stale", "commit", "not", "invalid")))
    charge_v1, charge_v2 = expected_charge(workspace, context), expected_charge(workspace, fresh)
    cancelled, cancel_evidence = budget_check(cancel.get(RECEIPT_NAMES[1]), context, charge_v1, "cancelled", 0, 0)
    settled, settled_evidence = budget_check(status.get(RECEIPT_NAMES[1]), fresh, charge_v2, "settled", 0, charge_v2)
    capsule_v2 = inspect_capsule(root / "commit-v2" / "reproducibility_capsule.zip", workspace, fresh)
    drift_clean = not any((root / "drift-commit" / name).exists() for name in RELEASE_OUTPUTS)
    weights = load_manifest("test_004")["assertions"]
    assertions: list[dict[str, Any]] = []
    award(assertions, weights, "CAPSULE.V1_STAGED", staged.get("valid", False), staged.get("evidence", ""))
    award(assertions, weights, "CAPSULE.V2_POLICY_BINDING", capsule_v2["valid"], capsule_v2["evidence"])
    award(assertions, weights, "CAPSULE.DRIFT_NO_PUBLICATION", drift_error[0] and drift_clean, f"{drift_error[1]}; no release={drift_clean}")
    award(assertions, weights, "BUDGET.INTEGER_ROUNDING", charge_v1 == 33 and charge_v2 == 38 and settled, f"v1={charge_v1}, v2={charge_v2}; {settled_evidence}")
    award(assertions, weights, "BUDGET.POLICY_DRIFT_REJECTED", drift_error[0], drift_error[1])
    award(assertions, weights, "BUDGET.USAGE_CONFLICT_REJECTED", usage_error[0], usage_error[1])
    award(assertions, weights, "BUDGET.CANCEL_AND_MIGRATE", cancelled and settled, f"cancel {cancel_evidence}; migrate {settled_evidence}")
    award(assertions, weights, "CROSS.OLD_REQUEST_FENCED", stale_error[0] and cancelled, f"{stale_error[1]}; {cancel_evidence}")
    award(assertions, weights, "CROSS.NEW_POLICY_ATOMIC", capsule_v2["valid"] and settled and commit["returncode"] == 0, "policy-v2 capsule and v2 settlement committed together")
    return assertions, runs, root / "commit-v2", fresh


def _case5(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    sessions, budgets = root / "sessions", root / "budgets"
    alpha = context
    beta = context_copy(context, tenant_id="tenant-beta", project_id="project-beta", run_manifest="run_manifest_beta.json", capsule_policy="capsule_policy_beta.json", usage_statement="usage_beta.json", budget_policy="budget_policy_beta.json", attestation_policy="attestation_policy_beta.json", notification_policy="notification_policy_beta.json")
    alpha_run = run_transaction(repo, workspace, root / "alpha", root / "alpha-ledger.json", sessions, budgets, alpha, "verify", timeout=timeout)
    beta_run = run_transaction(repo, workspace, root / "beta", root / "beta-ledger.json", sessions, budgets, beta, "verify", surface="launcher", timeout=timeout)
    alpha_status = run_transaction(repo, workspace, root / "alpha-status", root / "alpha-ledger.json", sessions, budgets, alpha, "status", timeout=timeout)
    beta_status = run_transaction(repo, workspace, root / "beta-status", root / "beta-ledger.json", sessions, budgets, beta, "status", timeout=timeout)
    intruder = context_copy(alpha, owner_id="unauthorized-actor")
    denied = run_transaction(repo, workspace, root / "denied", root / "intruder-ledger.json", sessions, budgets, intruder, "status", timeout=timeout)
    runs = [alpha_run, beta_run, alpha_status, beta_status, denied]
    alpha_capsule = inspect_capsule(root / "alpha" / "reproducibility_capsule.zip", workspace, alpha)
    beta_capsule = inspect_capsule(root / "beta" / "reproducibility_capsule.zip", workspace, beta)
    alpha_charge, beta_charge = expected_charge(workspace, alpha), expected_charge(workspace, beta)
    alpha_budget = budget_check(alpha_status.get(RECEIPT_NAMES[1]), alpha, alpha_charge, "settled", 0, alpha_charge)
    beta_budget = budget_check(beta_status.get(RECEIPT_NAMES[1]), beta, beta_charge, "settled", 0, beta_charge)
    denied_error = structured_error(denied, (("author", "permission", "forbidden"),))
    alpha_corpus = lower_corpus({"tx": alpha_status.get(RECEIPT_NAMES[0]), "budget": alpha_status.get(RECEIPT_NAMES[1])})
    beta_corpus = lower_corpus({"tx": beta_status.get(RECEIPT_NAMES[0]), "budget": beta_status.get(RECEIPT_NAMES[1])})
    denied_corpus = lower_corpus(denied.get("error.json"))
    status_established = alpha_status.get("returncode") == beta_status.get("returncode") == 0 and isinstance(alpha_status.get(RECEIPT_NAMES[0]), dict) and isinstance(beta_status.get(RECEIPT_NAMES[0]), dict)
    no_peer = status_established and "project-beta" not in alpha_corpus and "tenant-beta" not in alpha_corpus and "project-alpha" not in beta_corpus and "tenant-alpha" not in beta_corpus
    nondisclosure = all(token not in denied_corpus for token in ("project-beta", "tenant-beta", "operator-secret", "same-call"))
    secret_excluded = "operator_secret.txt" not in lower_corpus(alpha_capsule) and "operator_secret.txt" not in lower_corpus(beta_capsule)
    weights = load_manifest("test_005")["assertions"]
    assertions: list[dict[str, Any]] = []
    award(assertions, weights, "CAPSULE.ALPHA_VALID", alpha_capsule["valid"], alpha_capsule["evidence"])
    award(assertions, weights, "CAPSULE.BETA_VALID", beta_capsule["valid"], beta_capsule["evidence"])
    award(assertions, weights, "CAPSULE.SECRET_EXCLUDED", alpha_capsule["valid"] and beta_capsule["valid"] and secret_excluded, f"unselected operator secret absent={secret_excluded}")
    award(assertions, weights, "BUDGET.PROJECT_LOCAL_TOTALS", alpha_budget[0] and beta_budget[0], f"alpha {alpha_budget[1]}; beta {beta_budget[1]}")
    award(assertions, weights, "BUDGET.UNAUTHORIZED_NONDISCLOSURE", denied_error[0] and nondisclosure, f"{denied_error[1]}; peer details absent={nondisclosure}")
    award(assertions, weights, "BUDGET.PEER_STATUS_ISOLATION", no_peer, f"status established={status_established}; each project status excludes peer identifiers={no_peer}")
    independent = alpha_run["returncode"] == beta_run["returncode"] == 0 and find_key(alpha_run.get(RECEIPT_NAMES[0]), {"commit_id"}) != find_key(beta_run.get(RECEIPT_NAMES[0]), {"commit_id"})
    award(assertions, weights, "CROSS.COLLIDING_IDS_INDEPENDENT", independent, f"same subordinate IDs yielded distinct project/tenant commits={independent}")
    award(assertions, weights, "CROSS.NO_PEER_DISCLOSURE", no_peer and denied_error[0] and nondisclosure and secret_excluded, "established status/error and capsules exclude peer/secret material")
    return assertions, runs, root / "alpha", alpha


def _case6(prepared: dict[str, Any], workspace: Path, root: Path, context: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path, dict[str, Any]]:
    repo = Path(prepared["repository"])
    sessions, budgets, ledger = root / "sessions", root / "budgets", root / "ledger.json"
    budget_absent_before = not budgets.exists()
    ordinary = ordinary_probe(repo, workspace, root / "ordinary-scenario", root / "ordinary-ledger-scenario.json", timeout)
    ordinary_isolated = budget_absent_before and not budgets.exists() and release_outputs(root / "ordinary-scenario") is not None
    prepare = run_transaction(repo, workspace, root / "prepare", ledger, sessions, budgets, context, "prepare", timeout=timeout)
    cancel = run_transaction(repo, workspace, root / "cancel", ledger, sessions, budgets, context, "cancel", timeout=timeout)
    stale = run_transaction(repo, workspace, root / "stale-commit", ledger, sessions, budgets, context, "commit", timeout=timeout)
    fresh = context_copy(context, request_id="release-second", owner_id="worker-release", generation=2)
    commit = run_transaction(repo, workspace, root / "commit", ledger, sessions, budgets, fresh, "verify", surface="launcher", takeover_generation=1, timeout=timeout)
    commit_hashes = dict(commit["artifact_hashes"])
    commit_tx, commit_budget = commit.get(RECEIPT_NAMES[0]), commit.get(RECEIPT_NAMES[1])
    capsule_path = root / "commit" / "reproducibility_capsule.zip"
    if capsule_path.is_file():
        capsule_path.unlink()
    retry = run_transaction(repo, workspace, root / "retry", ledger, sessions, budgets, fresh, "verify", timeout=timeout)
    status = run_transaction(repo, workspace, root / "status", ledger, sessions, budgets, fresh, "status", timeout=timeout)
    runs = [ordinary, prepare, cancel, stale, commit, retry, status]
    charge = expected_charge(workspace, context)
    cancelled = budget_check(cancel.get(RECEIPT_NAMES[1]), context, charge, "cancelled", 0, 0)
    stale_error = structured_error(stale, (("cancel", "state", "phase"), ("stale", "commit", "invalid", "not")))
    settled = budget_check(status.get(RECEIPT_NAMES[1]), fresh, charge, "settled", 0, charge)
    capsule = inspect_capsule(root / "retry" / "reproducibility_capsule.zip", workspace, fresh)
    repaired = commit_hashes == retry["artifact_hashes"] and set(commit_hashes) == set(RELEASE_OUTPUTS)
    ids = stable_ids(commit_tx, commit_budget, retry.get(RECEIPT_NAMES[0]), retry.get(RECEIPT_NAMES[1]))
    weights = load_manifest("test_006")["assertions"]
    assertions: list[dict[str, Any]] = []
    prepare_clean = not any((root / "prepare" / name).exists() for name in RELEASE_OUTPUTS)
    cancel_established = cancel.get("returncode") == 0 and isinstance(cancel.get(RECEIPT_NAMES[1]), dict)
    award(assertions, weights, "CAPSULE.CANCELLED_NOT_PUBLISHED", prepare_clean and cancel_established and stale_error[0], f"prepare published no release={prepare_clean}; cancel established={cancel_established}; {stale_error[1]}")
    award(assertions, weights, "CAPSULE.REENTRY_VALID", capsule["valid"], capsule["evidence"])
    award(assertions, weights, "CAPSULE.REPAIRED_EXACT_BYTES", repaired, f"removed response artifact restored with exact committed artifact hashes={repaired}")
    award(assertions, weights, "BUDGET.CANCEL_RELEASES", cancelled[0], cancelled[1])
    award(assertions, weights, "BUDGET.STALE_COMMIT_REJECTED", cancel_established and stale_error[0], f"cancel established={cancel_established}; {stale_error[1]}")
    award(assertions, weights, "BUDGET.SINGLE_FINAL_SETTLEMENT", settled[0] and ids, f"{settled[1]}; stable IDs={ids}")
    award(assertions, weights, "CROSS.ORDINARY_COMPATIBILITY", ordinary["returncode"] == 0 and ordinary_isolated, f"ordinary artifacts valid and budget store untouched={ordinary_isolated}")
    award(assertions, weights, "CROSS.RECONCILIATION_NO_RECHARGE", repaired and ids and settled[0], f"artifact repaired={repaired}, IDs stable={ids}; {settled[1]}")
    return assertions, runs, root / "retry", fresh


SCENARIOS = {
    "test_001": _case1,
    "test_002": _case2,
    "test_003": _case3,
    "test_004": _case4,
    "test_005": _case5,
    "test_006": _case6,
}


def execute_case(prepared: dict[str, Any], case_id: str, work_dir: Path, timeout: int = 600) -> dict[str, Any]:
    if case_id not in SCENARIOS or case_id not in SPECS:
        raise HarnessError(f"unknown hidden case {case_id}")
    manifest = load_manifest(case_id)
    root = work_dir / "cases" / case_id
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    workspace = root / "workspace"
    case_root = BENCHMARK_ROOT / "test_cases" / case_id
    shutil.copytree(case_root / "assets", workspace)
    context = load_context(case_root)
    repo = Path(prepared["repository"])
    before_workspace, before_repo = tree_digest(workspace), tree_digest(repo)
    operation_timeout = min(70, timeout)
    ordinary = ordinary_probe(repo, workspace, root / "ordinary-probe", root / "ordinary-probe-ledger.json", operation_timeout)
    protocol_assertions, protocol_runs, science_output, science_context = SCENARIOS[case_id](prepared, workspace, root, context, operation_timeout)
    if release_outputs(science_output) is None:
        science_output = root / "ordinary-probe"
        science_context = context
    weights = manifest["assertions"]
    assertions: list[dict[str, Any]] = []
    bindings, evidence, science_evidence = science_checks(SPECS[case_id], science_output)
    award(assertions, weights, "SCIENCE.CLAIMS_BINDINGS", bindings, science_evidence)
    award(assertions, weights, "SCIENCE.DECISION_EVIDENCE", evidence, science_evidence)
    claim_contract_ok, claim_contract_detail = scientific_claim_contract(SPECS[case_id], science_output, workspace)
    award(assertions, weights, "SCIENCE.CLAIM_CONTRACT", claim_contract_ok, claim_contract_detail)
    assertions.extend(protocol_assertions)
    aux_output = next((path.parent for path in sorted(root.rglob("attestation.json")) if (path.parent / "notification_receipt.json").is_file() and (path.parent / "transaction_receipt.json").is_file() and all((path.parent / name).is_file() for name in RELEASE_OUTPUTS)), science_output)
    att_bound, att_digest, note_bound, note_durable, aux_evidence = auxiliary_checks(aux_output, science_context)
    award(assertions, weights, "ATTESTATION.BOUND_AND_DURABLE", att_bound, aux_evidence)
    award(assertions, weights, "ATTESTATION.DIGEST_FENCING", att_digest, aux_evidence)
    award(assertions, weights, "OUTBOX.EVENT_BOUND_AND_IDEMPOTENT", note_bound, aux_evidence)
    award(assertions, weights, "OUTBOX.DURABLE_RETRY", note_durable, aux_evidence)
    integrated, integration_evidence = integration_score(repo, set(prepared["changed_paths"]))
    award(assertions, weights, "INTEGRATION.PRODUCTION_PATHS", integrated, integration_evidence)
    provenance = inspect_provenance(science_output, workspace, science_context)
    provenance_stable, provenance_evidence = provenance_retry_stable(root, provenance.get("journal_id"), science_output)
    award(assertions, weights, "PROVENANCE.MANIFEST_BINDING", provenance["binding"], provenance["evidence"])
    award(assertions, weights, "PROVENANCE.WORKER_DECISIONS", provenance["workers"], provenance["evidence"])
    award(assertions, weights, "PROVENANCE.FAILURE_POLICY", provenance["policy"], provenance["evidence"])
    award(assertions, weights, "PROVENANCE.CLEANUP_SCOPE", provenance["cleanup"], provenance["evidence"])
    award(assertions, weights, "PROVENANCE.RETRY_DURABLE", provenance_stable, provenance_evidence)
    ids = [item["id"] for item in assertions]
    if len(ids) != len(set(ids)) or set(ids) != set(weights) or sum(item["possible"] for item in assertions) != 100:
        raise HarnessError(f"runtime assertions do not exactly match manifest for {case_id}")
    runs = [ordinary] + protocol_runs
    if any(run.get("timed_out") or run.get("memory_exceeded") for run in runs):
        return {"case_id": case_id, "valid": False, "score": 0, "failure": "product operation exceeded the generous time or process-tree RSS limit"}
    input_unchanged = before_workspace == tree_digest(workspace) and before_repo == tree_digest(repo)
    score = sum(item["earned"] for item in assertions)
    gate_ok, gate_evidence = science_gate_check(science_output, workspace)
    semantic_gate_ok = gate_ok and claim_contract_ok
    if not semantic_gate_ok:
        score = min(score, 35)
    ceiling = None
    if not input_unchanged:
        ceiling = int(manifest["integrity_ceiling"])
        score = min(score, ceiling)
    return {
        "case_id": case_id,
        "valid": True,
        "score": score,
        "assertions": assertions,
        "ceiling_applied": ceiling,
        "integrity_ceiling_applied": not input_unchanged,
        "science_gate": {"passed": semantic_gate_ok, "evidence": gate_evidence + "; " + claim_contract_detail},
        "safety_ceiling_applied": 35 if not semantic_gate_ok else None,
        "runtime_seconds": round(sum(run.get("runtime_seconds", 0) for run in runs), 4),
        "peak_memory_bytes": max((run.get("peak_memory_bytes", 0) for run in runs), default=0),
        "ordinary_probe_returncode": ordinary["returncode"],
        "operation_summary": [{"operation": run.get("operation"), "returncode": run.get("returncode"), "timed_out": run.get("timed_out", False), "memory_exceeded": run.get("memory_exceeded", False)} for run in runs],
    }
