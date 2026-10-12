#!/usr/bin/env python3
"""Shared-within-a-sibling formal publisher for the 0905 Edit repair.

Deterministic code in this module only validates provenance, classifies
infrastructure failures, and prepares sanitized evidence.  It never computes a
semantic Result score.  Every scoreable hidden case is sent exactly once to the
evaluator-owned shared Result judge.  The frozen Candidate is sent once to the
authoritative Create Code judge.  The two axes remain independent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CASES = tuple(f"test_{i:03d}" for i in range(1, 7))
SHARED_RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
# The fixed wrapper delegates to the authoritative Create Code code_eval.py;
# Candidate code cannot select or replace that implementation.
INFRA_MARKERS = (
    "infrastructure", "provider_failure", "provider_error", "broker_failure",
    "broker_error", "credential_error", "mount_error", "docker_error",
    "launcher_infrastructure", "evaluator_error", "runtime_dependency",
)
SECRET_KEYS = ("credential", "api_key", "authorization", "secret", "token", "judge_prompt")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    """Match the Create code judge's immutable-tree digest contract."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            digest.update(b"F")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            continue
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sanitized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitized(item)
            for key, item in value.items()
            if not any(marker in key.lower() for marker in SECRET_KEYS)
        }
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, str):
        if value.startswith("Bearer ") or "DEEPSEEK_API_KEY=" in value or "OPENAI_API_KEY=" in value:
            return "<redacted>"
        return value[-20000:]
    return value


def is_infrastructure(record: dict[str, Any]) -> bool:
    if record.get("infrastructure_invalid") is True or record.get("infra_valid") is False:
        return True
    axis = str(record.get("classification_axis", record.get("failure_attribution", {}).get("axis", ""))).lower()
    classification = str(record.get("classification", "")).lower()
    return axis == "infrastructure" or any(marker in classification for marker in INFRA_MARKERS)


def provider_usage_errors(usage: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{label} {field} must be a positive integer")
    attempts = usage.get("transport_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        errors.append(f"{label} transport_attempts must be a positive integer")
    if all(isinstance(usage.get(field), int) and not isinstance(usage.get(field), bool) for field in ("input_tokens", "output_tokens", "total_tokens")) and usage["total_tokens"] < max(usage["input_tokens"], usage["output_tokens"]):
        errors.append(f"{label} total_tokens is inconsistent with input/output usage")
    return errors


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def layout_paths(root: Path, run: Path, layout: str) -> tuple[Path, Path, dict[str, Path]]:
    if layout == "openhands":
        freeze = run / "lifecycle" / "freeze_manifest.json"
        frozen = run / "lifecycle" / "frozen_candidate"
        records = {case: run / "lifecycle" / "hidden" / case / "case_result.json" for case in CASES}
        # Records written before the controller carried `candidate_digest` are
        # materialised with the launcher's own candidate_source_digest (the frozen
        # tree the case ran against); the original file stays byte-identical so the
        # hidden attestation sha still binds it, and the derivation is recorded.
        import hashlib as _hashlib
        for case, path in list(records.items()):
            if not path.is_file():
                continue
            value = read_json(path)
            if not isinstance(value, dict) or value.get("candidate_digest"):
                continue
            source_digest = (value.get("launcher") or {}).get("candidate_source_digest")
            if not is_sha256(source_digest):
                continue
            materialized = run / "formal_scoring" / "native_records" / f"{case}.json"
            derived = dict(value, candidate_digest=source_digest, record_derivation={
                "source": str(path), "source_sha256": _hashlib.sha256(path.read_bytes()).hexdigest(),
                "added": ["candidate_digest"], "candidate_digest_from": "launcher.candidate_source_digest"})
            write_json(materialized, derived)
            records[case] = materialized
    elif layout == "openclaw":
        freeze = run / "lifecycle" / "freeze_manifest.json"
        value = read_json(freeze) if freeze.is_file() else {}
        frozen = Path(str(value.get("frozen_candidate_path", run / "lifecycle" / "frozen_candidate")))
        records = {case: run / "hidden" / case / "case_result.json" for case in CASES}
        suite = run / "hidden" / "hidden_result.json"
        if suite.is_file():
            suite_value = read_json(suite)
            for case, record in suite_value.items():
                if case in records and isinstance(record, dict):
                    materialized = run / "formal_scoring" / "native_records" / f"{case}.json"
                    write_json(materialized, record)
                    records[case] = materialized
    elif layout == "ai_scientist":
        freeze = run / "lifecycle" / "freeze_manifest.json"
        value = read_json(freeze) if freeze.is_file() else {}
        frozen = Path(str(value.get("frozen_candidate_path", run / "lifecycle" / "frozen_candidate")))
        hidden = run / "lifecycle" / "hidden-after-freeze-attestation.json"
        records = {}
        if hidden.is_file():
            for item in read_json(hidden).get("cases", []):
                if isinstance(item, dict) and item.get("case_id") in CASES:
                    result = Path(str(item.get("result_path", "")))
                    records[str(item["case_id"])] = result
        for case in CASES:
            records.setdefault(case, run / "lifecycle" / "hidden" / case / "launcher_result.json")
    else:
        raise ValueError(f"unknown layout: {layout}")
    return freeze, frozen, records


def lifecycle_errors(run: Path, freeze: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    lifecycle_candidates = (run / "lifecycle" / "dev_lifecycle.json", run / "dev_lifecycle.json")
    lifecycle_path = next((path for path in lifecycle_candidates if path.is_file()), None)
    if lifecycle_path is None:
        return ["missing dev_lifecycle.json"]
    lifecycle = read_json(lifecycle_path)
    records = lifecycle.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 10:
        errors.append("accepted submissions must contain 1..10 records")
        records = []
    if lifecycle.get("evidence_kind") in {"pilot", "smoke"} or lifecycle.get("pilot_not_formal") is True:
        errors.append("pilot/smoke lifecycle cannot publish formal scores")
    if lifecycle.get("evidence_kind") not in {None, "formal"}:
        errors.append("formal aggregation requires formal evidence")
    maximum = lifecycle.get("max_dev_rounds")
    if maximum is None:
        maximum = freeze.get("max_dev_rounds")
    if not isinstance(maximum, int) or not 1 <= maximum <= 10:
        errors.append("max_dev_rounds must be recorded in 1..10")
    if lifecycle.get("n_concurrent", freeze.get("n_concurrent")) != 1:
        errors.append("n_concurrent must equal 1")
    for index, record in enumerate(records, 1):
        dev = record.get("dev") if isinstance(record, dict) else None
        if not isinstance(dev, dict) or set(dev) != {"dev_001", "dev_002"}:
            errors.append(f"accepted submission {index} does not contain exactly dev_001+dev_002")
        if not isinstance(record, dict):
            continue
        if record.get("submission", record.get("submission_number")) != index:
            errors.append(f"accepted submission {index} is not sequentially numbered")
        if not is_sha256(record.get("candidate_digest")):
            errors.append(f"accepted submission {index} has an invalid candidate digest")
        if not is_sha256(record.get("feedback_digest")):
            errors.append(f"accepted submission {index} has no fresh feedback digest")
        if index > 1 and record.get("feedback_digest_ack") != records[index - 2].get("feedback_digest"):
            errors.append(f"accepted submission {index} does not acknowledge the prior feedback digest")
        for case_id in ("dev_001", "dev_002"):
            result = dev.get(case_id) if isinstance(dev, dict) else None
            if isinstance(result, dict):
                if result.get("real_execution") is False:
                    errors.append(f"accepted submission {index} {case_id} was not real execution")
                if is_infrastructure(result):
                    errors.append(f"accepted submission {index} {case_id} is infrastructure-invalid")
    source_submission = freeze.get("source_submission")
    if records and source_submission not in {len(records), f"candidate-{len(records):03d}", f"candidate_{len(records):03d}"}:
        errors.append("freeze is not bound to the latest accepted submission")
    reason = freeze.get("freeze_reason")
    if reason not in {"max_dev_rounds", "builder_exit"}:
        errors.append("freeze_reason must be max_dev_rounds or builder_exit")
    if lifecycle.get("dev_passed_is_automatic_freeze", freeze.get("dev_passed_is_automatic_freeze")) is not False:
        errors.append("dev_passed must be record-only and never automatic freeze")
    if lifecycle.get("pilot_not_formal") is True or lifecycle.get("evidence_kind") in {"pilot", "smoke"}:
        errors.append("pilot/smoke lifecycle cannot publish formal scores")
    digests = [record.get("candidate_digest") for record in records if isinstance(record, dict)]
    if len(digests) != len(set(digests)):
        errors.append("accepted candidate digests must be distinct")
    if freeze.get("accepted_submission_count") != len(records):
        errors.append("freeze accepted_submission_count does not match accepted ledger")
    if freeze.get("accepted_candidate_digests") != digests:
        errors.append("freeze accepted_candidate_digests does not match accepted ledger")
    if records and freeze.get("candidate_delivery_digest") != digests[-1]:
        errors.append("freeze candidate_delivery_digest is not the latest accepted delivery")
    if freeze.get("feedback_consumed") is not True or freeze.get("feedback_chain_complete") is not True:
        errors.append("freeze does not prove complete feedback consumption")
    if freeze.get("dev_evaluated") is not True:
        errors.append("freeze does not prove dev evaluation")
    return errors


def artifact_for(layout: str, run: Path, case_id: str, record_path: Path, record: dict[str, Any]) -> Path:
    base = record_path.parent
    candidates: list[Path] = []
    authored = record.get("authored_artifact")
    if isinstance(authored, str):
        candidates.append(Path(authored))
    output_path = record.get("output_path")
    if isinstance(output_path, str):
        candidates.append(Path(output_path) / "agent_result.json")
    candidates.extend((base / "agent_result.json", run / "hidden" / case_id / "agent_result.json", run / "lifecycle" / "hidden" / case_id / "agent_result.json"))
    artifact = next((path.resolve() for path in candidates if path.is_file()), None)
    if artifact is None:
        raise ValueError("model-authored agent_result.json is missing")
    case_root = next((parent for parent in (run / "hidden" / case_id, run / "lifecycle" / "hidden" / case_id, base) if parent.is_dir() and inside(artifact, parent)), None)
    if case_root is None or not inside(artifact, run):
        raise ValueError("agent artifact is not case-local run evidence")
    value = read_json(artifact)
    if value.get("case_id") != case_id:
        raise ValueError("agent artifact case_id mismatch")
    if value.get("evaluator_synthesized") is True or value.get("source") == "evaluator":
        raise ValueError("evaluator-synthesized artifact is forbidden")
    if value.get("real_execution") is False:
        raise ValueError("agent artifact is not from real lower execution")
    protocol = value.get("model_protocol") if isinstance(value.get("model_protocol"), dict) else value
    if protocol.get("model") != "deepseek-flash" or protocol.get("reasoning_effort") != "high":
        raise ValueError("agent artifact is not bound to deepseek-flash/medium lower execution")
    embedded = record.get("agent_result")
    if isinstance(embedded, dict) and embedded != value:
        raise ValueError("agent artifact differs from controller-captured artifact")
    return artifact


def evidence_files(root: Path, run: Path, layout: str, case_id: str, record_path: Path, record: dict[str, Any], artifact: Path) -> tuple[Path, Path, Path]:
    out = run / "formal_scoring" / "result_axis" / case_id / "inputs"
    if layout == "openhands":
        # Public and hidden judgments require the same completed model origin,
        # frozen product, actual case world, and trajectory/artifact binding.
        from case_evidence import prepare_case_evidence
        candidate = run / "lifecycle" / "frozen_candidate"
        evidence = prepare_case_evidence(
            case_id=case_id, record_path=record_path, candidate=candidate,
            candidate_digest=tree_digest(candidate), output=out)
        if evidence["artifact"] != artifact or evidence["raw_trajectory"] is None:
            raise ValueError("OpenHands formal artifact or trajectory binding is incomplete")
        return evidence["raw_trajectory"], evidence["native_evidence"], evidence["private_oracle"]
    out.mkdir(parents=True, exist_ok=True)
    trajectory_candidates = [
        record_path.parent / "trajectory.json", record_path.parent / "launcher_request.json",
        record_path.parent / "controller_execution.json", record_path.parent / "controller_observation.json",
        record_path.parent / "launcher_result.json",
    ]
    trajectory = {
        "case_id": case_id,
        "events": [sanitized(read_json(path)) for path in trajectory_candidates if path.is_file() and path != artifact],
        "record_projection": sanitized(record),
    }
    native = {"case_id": case_id, "classification": record.get("classification"), "classification_axis": record.get("classification_axis"), "facts": sanitized({key: value for key, value in record.items() if key not in {"agent_result", "authored_artifact"}})}
    dynamic_sources: list[dict[str, Any]] = []
    for path in (
        root / "test_cases" / case_id / "assets" / "fixtures.json",
        record_path.parent / "private_facts.json",
        record_path.parent / "fresh_workspace" / "transaction_context.json",
        root / "test_cases" / case_id / "assets" / "transaction_context.json",
    ):
        if path.is_file():
            dynamic_sources.append({"name": path.name, "sha256": sha256_file(path), "keys": sorted(read_json(path))})
    oracle = {
        "case_id": case_id,
        "comparison_only": True,
        "artifact_sha256": sha256_file(artifact),
        "record_case_matches": record.get("case_id") in {None, case_id},
        "frozen_digest_stable": record.get("frozen_candidate_digest_stable", record.get("freeze_proof", {}).get("digest_stable", True)),
        "dynamic_sources": dynamic_sources,
        "provenance_credit_rule": "wrong case, generation, tenant, route, rollout, or receipt receives no provenance credit",
    }
    trajectory_path, native_path, oracle_path = out / "trajectory.json", out / "native_evidence.json", out / "oracle_summary.json"
    write_json(trajectory_path, trajectory); write_json(native_path, native); write_json(oracle_path, oracle)
    return trajectory_path, native_path, oracle_path


def task_rubric(root: Path, run: Path, case_id: str) -> Path:
    generic = root / "evaluator" / "result_rubric.md"
    contract_path = root / "meta" / "0905_case_contract.json"
    contract = read_json(contract_path) if contract_path.is_file() else {}
    case = (contract.get("cases") or {}).get(case_id, {}) if isinstance(contract.get("cases"), dict) else {}
    text = (generic.read_text(encoding="utf-8") if generic.is_file() else "# Result rubric\n")
    text += "\n## Case-local interpretation\n"
    text += f"Primary user goal: {case.get('natural_user_goal', 'complete the supplied task')}\n\n"
    text += f"Primary failure axis: {case.get('primary_failure_axis', 'task-specific recovery')}\n\n"
    text += "Award no provenance credit for a foreign case, generation, tenant, route, rollout, or receipt. Field presence is not semantic success.\n"
    path = run / "formal_scoring" / "result_axis" / case_id / "inputs" / "task_rubric.md"
    path.write_text(text, encoding="utf-8")
    return path


def validate_result_contract(path: Path, case_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = read_json(path)
    except Exception as exc:
        return None, f"invalid Result contract: {type(exc).__name__}: {exc}"
    usage = value.get("provider_usage") if isinstance(value.get("provider_usage"), dict) else {}
    judge = value.get("judge") if isinstance(value.get("judge"), dict) else {}
    usage_errors = provider_usage_errors(usage, "Result judge")
    checks = (
        value.get("contract_valid") is True,
        value.get("result_score_publishable") is True,
        value.get("case_id") == case_id,
        judge.get("model") == "deepseek-flash",
        judge.get("reasoning_effort") == "max",
        usage.get("logical_requests") == (2 if value.get("early_stop_resample") else 1),
        usage.get("completed_responses") == (2 if value.get("early_stop_resample") else 1),
        isinstance(value.get("result_score"), int) and not isinstance(value.get("result_score"), bool) and 0 <= value["result_score"] <= 100,
    )
    return (value, None) if all(checks) and not usage_errors else (None, "Result judge contract failed the model/effort/usage/score gate: " + "; ".join(usage_errors))


def stage_requirements(root: Path, output: Path) -> Path:
    target = output / "public_requirements"
    target.mkdir(parents=True, exist_ok=True)
    sources = sorted((root / "input").glob("*.md"))
    if not sources:
        raise FileNotFoundError("public requirement markdown files are missing")
    for source in sources:
        shutil.copy2(source, target / source.name)
    return target


def validate_code_contract(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = read_json(path)
    except Exception as exc:
        return None, f"invalid Code contract: {type(exc).__name__}: {exc}"
    judge = value.get("judge") if isinstance(value.get("judge"), dict) else {}
    usage = value.get("provider_usage") if isinstance(value.get("provider_usage"), dict) else {}
    usage_errors = provider_usage_errors(usage, "Code judge")
    checks = (
        value.get("contract_valid") is True,
        value.get("code_score_publishable") is True,
        judge.get("model") == "deepseek-flash",
        judge.get("reasoning_effort") == "max",
        usage.get("logical_requests") == 1,
        usage.get("completed_responses") == 1,
        isinstance(value.get("code_score"), int) and not isinstance(value.get("code_score"), bool) and 0 <= value["code_score"] <= 100,
    )
    return (value, None) if all(checks) and not usage_errors else (None, "Code judge contract failed the model/effort/usage/score gate: " + "; ".join(usage_errors))


def _legacy_finalize_unused(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=("openhands", "openclaw", "ai_scientist"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--result-broker-endpoint", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--result-judge", type=Path, default=SHARED_RESULT_JUDGE)
    parser.add_argument("--code-judge", type=Path, default=CREATE_CODE_JUDGE)
    parser.add_argument("--code-rubric", type=Path, required=True)
    parser.add_argument("--judge-timeout", type=int, default=900)
    args = parser.parse_args(argv)
    # Compatibility flags are accepted only so older one-stop callers do not
    # fail; judge authority is evaluator-owned and cannot be redirected.
    args.result_judge = SHARED_RESULT_JUDGE
    args.code_judge = CREATE_CODE_JUDGE
    root = Path(__file__).resolve().parents[1]
    run = args.run_dir.resolve()
    output = (args.output or run / "formal_aggregation.json").resolve()
    aggregation: dict[str, Any] = {
        "schema_version": "agentswe-edit-formal-aggregation-0905-v1",
        "formal_result_publishable": False, "code_score_publishable": False,
        "result_axis": "N/A", "code_axis": "N/A", "combined_score": None,
        "result_judge_contracts": {}, "code_contract": None, "reasons": [],
    }
    try:
        freeze_path, frozen, records = layout_paths(root, run, args.layout)
        if not freeze_path.is_file():
            raise FileNotFoundError(f"freeze manifest missing: {freeze_path}")
        freeze = read_json(freeze_path)
        aggregation["reasons"].extend(lifecycle_errors(run, freeze))
        expected_digest = str(freeze.get("candidate_digest", freeze.get("candidate_materialized_digest", "")))
        if not frozen.is_dir() or not inside(frozen, run):
            aggregation["reasons"].append("frozen Candidate is missing or outside the run")
        elif not expected_digest or tree_digest(frozen) != expected_digest:
            aggregation["reasons"].append("frozen Candidate digest is missing or changed")

        scores: dict[str, int] = {}
        for case_id in CASES:
            record_path = records.get(case_id)
            if record_path is None or not record_path.is_file() or not inside(record_path, run):
                aggregation["reasons"].append(f"{case_id}: native case record missing")
                continue
            record = read_json(record_path)
            if record.get("case_id") not in {None, case_id}:
                aggregation["reasons"].append(f"{case_id}: native record case mismatch")
                continue
            if is_infrastructure(record):
                aggregation["result_judge_contracts"][case_id] = {"classification": "infrastructure-invalid", "score": "N/A", "invoked": False}
                aggregation["reasons"].append(f"{case_id}: infrastructure-invalid; rerun required")
                continue
            try:
                artifact = artifact_for(args.layout, run, case_id, record_path, record)
                trajectory, native, oracle = evidence_files(root, run, args.layout, case_id, record_path, record, artifact)
                rubric = task_rubric(root, run, case_id)
                judge_dir = run / "formal_scoring" / "result_axis" / case_id / "judge"
                command = [
                    sys.executable, str(args.result_judge.resolve()), "--case-id", case_id,
                    "--task-input", str(root / "test_cases" / case_id / (
                        "natural_task.md" if args.layout == "openhands"
                        and (root / "test_cases" / case_id / "natural_task.md").is_file()
                        else "input.md")),
                    "--rubric", str(rubric), "--agent-artifact", str(artifact),
                    "--trajectory", str(trajectory), "--native-evidence", str(native),
                    "--oracle-summary", str(oracle), "--broker-endpoint", args.result_broker_endpoint,
                    "--broker-placeholder", "broker-only-placeholder",
                    "--output-dir", str(judge_dir), "--timeout", str(args.judge_timeout),
                ]
                completed = subprocess.run(command, text=True, capture_output=True, check=False)
                contract_path = judge_dir / "result_score_contract.json"
                contract, error = validate_result_contract(contract_path, case_id)
                if completed.returncode != 0 or error or contract is None:
                    aggregation["reasons"].append(f"{case_id}: {error or 'shared Result judge refused publication'}")
                    aggregation["result_judge_contracts"][case_id] = str(contract_path)
                    continue
                scores[case_id] = int(contract["result_score"])
                aggregation["result_judge_contracts"][case_id] = str(contract_path)
            except Exception as exc:
                aggregation["reasons"].append(f"{case_id}: artifact/evidence rejection: {type(exc).__name__}: {exc}")

        if set(scores) == set(CASES):
            aggregation["formal_result_publishable"] = True
            aggregation["result_axis"] = {
                "score": round(sum(scores.values()) / 6, 4), "maximum": 100,
                "aggregation": "arithmetic mean of six independently judged hidden cases",
                "case_scores": scores,
            }

        if frozen.is_dir() and expected_digest and tree_digest(frozen) == expected_digest:
            code_dir = run / "formal_scoring" / "code_axis"
            requirements = stage_requirements(root, run / "formal_scoring")
            command = [
                sys.executable, str(args.code_judge.resolve()), "--candidate-source", str(frozen),
                "--public-requirements", str(requirements), "--code-rubric", str(args.code_rubric.resolve()),
                "--credential-file", str(args.credential_file.resolve()), "--output-dir", str(code_dir),
                "--expected-candidate-digest", expected_digest, "--timeout", str(args.judge_timeout),
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            code_path = code_dir / "code_score_contract.json"
            code, error = validate_code_contract(code_path)
            aggregation["code_contract"] = str(code_path)
            if completed.returncode == 0 and code is not None and error is None:
                aggregation["code_score_publishable"] = True
                aggregation["code_axis"] = {"score": code["code_score"], "maximum": 100, "contract": str(code_path)}
            else:
                aggregation["reasons"].append(error or "Create Code judge refused publication")
    except Exception as exc:
        aggregation["reasons"].append(f"formal finalization error: {type(exc).__name__}: {exc}")
    aggregation["source_evidence"] = {"run_dir": str(run), "layout": args.layout}
    write_json(output, aggregation)
    print(json.dumps(aggregation, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if aggregation["formal_result_publishable"] and aggregation["code_score_publishable"] else 2


def finalize(argv: list[str] | None = None) -> int:
    from shared_finalize import finalize as shared_finalize
    return shared_finalize(argv)


if __name__ == "__main__":
    raise SystemExit(finalize())
