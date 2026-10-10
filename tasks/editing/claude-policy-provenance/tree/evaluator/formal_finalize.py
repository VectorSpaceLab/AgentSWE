#!/usr/bin/env python3
"""Publish formal Claude Result/Code axes from one complete run-local lifecycle.

The finalizer is intentionally read-only with respect to Candidate execution. It
does not start a broker, provider, Docker, Harbor, or a lower agent. Static
audits, probes, replays, partial inventories, and infrastructure-invalid runs
remain unscored.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentloop.evaluator.hidden_executor import CASE_IDS, candidate_digest, validate_freeze  # noqa: E402
from agentloop.protocol import MODEL, REASONING_EFFORT  # noqa: E402
from agentloop.evaluator.semantic_score import execution_verdict, judge_evidence, SemanticMeasurementUnavailable


SCHEMA = "agentswe-claude-formal-aggregation/v1"
RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
# This evaluator-owned wrapper delegates to the authoritative Create Code
# implementation at @@AGENTSWE_EDITING_CONTROL@@/code_eval.py.
CODE_WEIGHTS = {
    "interface_lifecycle": 15,
    "requirement_mechanism_coverage": 20,
    "analysis_evidence_integrity": 15,
    "safety_privacy_side_effects": 15,
    "recovery_honest_failure": 10,
    "testability_observability": 10,
    "maintainability_generalization": 10,
    "resource_discipline": 5,
}
INFRA_CLASSIFICATIONS = {
    "infrastructure-invalid",
    "broker_infrastructure_error",
    "broker_infrastructure_failure",
    "provider_failure",
    "provider_infrastructure_failure",
    "credential_infrastructure_failure",
    "launcher_infrastructure_failure",
    "launcher_or_trajectory_failure",
    "evaluator_infrastructure_failure",
    "mount_infrastructure_failure",
    "infra_case_spec_unavailable",
    "malformed_trajectory_schema",
}


def valid_judge_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    if not all(isinstance(usage.get(field), int) and not isinstance(usage.get(field), bool) and usage[field] > 0
               for field in ("input_tokens", "output_tokens", "total_tokens")):
        return False
    attempts = usage.get("transport_attempts")
    return (isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 1
            and usage["total_tokens"] >= max(usage["input_tokens"], usage["output_tokens"]))


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def inside(run_dir: Path, value: str | Path, *, base: Path | None = None) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (base or run_dir) / candidate
    resolved = candidate.resolve()
    if resolved != run_dir and run_dir not in resolved.parents:
        raise ValueError(f"evidence path escapes run directory: {resolved}")
    return resolved


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_tree_digest(root: Path) -> str:
    """Create's full-source identity, separately bound to the task identity.

    The task digest deliberately includes directory entries but excludes the
    evaluator build manifest. Create hashes all files and symlink targets and
    skips directories. Neither identity is substituted for the other.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, data = b'L', os.readlink(path).encode()
        elif path.is_file():
            kind, data = b'F', path.read_bytes()
        elif path.is_dir():
            continue
        else:
            kind, data = b'O', b''
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, 'big'))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def prepare_code_inputs(run_dir: Path, frozen: Path, frozen_digest: str) -> tuple[Path, str]:
    """Copy every public input document; baseline code is already in Candidate."""
    if candidate_digest(frozen) != frozen_digest:
        raise ValueError('task source identity changed before Code input preparation')
    requirements = run_dir / 'formal_scoring/code_public_requirements'
    requirements.mkdir(parents=True, exist_ok=True)
    sources = sorted((ROOT / 'input').glob('*.md'))
    if {p.name for p in sources} != {'01_task_goal.md', '02_interface_and_delivery.md',
                                   '03_requirements_and_constraints.md', '04_resources.md'}:
        raise ValueError('unexpected public requirements inventory')
    manifest = {}
    for source in sources:
        target = requirements / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError('public Code requirements changed in this run')
        if not target.exists():
            shutil.copyfile(source, target)
            target.chmod(0o444)
        manifest[source.name] = {'source': str(source), 'sha256': sha256(source)}
    code_digest = code_tree_digest(frozen)
    binding = {
        'candidate_source': str(frozen), 'task_candidate_digest': frozen_digest,
        'code_full_tree_digest': code_digest, 'public_requirements': manifest,
        'task_digest_implementation': {'path': str(ROOT / 'agentloop/protocol.py'),
            'sha256': sha256(ROOT / 'agentloop/protocol.py')},
        'code_digest_implementation': {'path': str(Path(__file__).resolve()),
            'sha256': sha256(Path(__file__).resolve()),
            'function_sha256': hashlib.sha256(inspect.getsource(code_tree_digest).encode()).hexdigest()},
        'authoritative_create_implementation': {'path': '@@AGENTSWE_EDITING_CONTROL@@/code_eval.py',
            'sha256': sha256(Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py'))
                if Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py').is_file() else None},
        'source_projection': False, 'requirements_projection': 'all four public input Markdown documents',
    }
    binding_path = run_dir / 'formal_scoring/code_source_binding.json'
    if binding_path.is_symlink():
        raise ValueError('Code source binding may not be a symlink')
    if binding_path.exists():
        if read_object(binding_path) != binding:
            raise ValueError('Code source binding changed in this run')
    else:
        write_json(binding_path, binding)
        binding_path.chmod(0o444)
    return requirements, code_digest


def runtime(value: dict[str, Any] | None) -> dict[str, int]:
    source = value.get("runtime") if isinstance(value, dict) else None
    source = source if isinstance(source, dict) else {}
    calls = int(source.get("calls", 0) or 0)
    failures = int(source.get("failures", 0) or 0)
    successful = int(source.get("successful_calls", calls - failures) or 0)
    return {
        "calls": calls,
        "failures": failures,
        "successful_calls": successful,
        "broker_failures": int(source.get("broker_failures", 0) or 0),
        "provider_failures": int(source.get("provider_failures", 0) or 0),
        "credential_failures": int(source.get("credential_failures", 0) or 0),
        "protocol_failures": int(source.get("protocol_failures", 0) or 0),
    }


def delta(after: dict[str, Any] | None, before: dict[str, Any] | None) -> dict[str, int]:
    left, right = runtime(before), runtime(after)
    return {key: right[key] - left[key] for key in left}


def rejection_payload(reasons: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "formal_result_publishable": False,
        "code_score_publishable": False,
        "result_axis": "N/A",
        "code_axis": "N/A",
        "reasons": reasons,
    }


def run_marker_reasons(run_dir: Path) -> list[str]:
    reasons: list[str] = []
    for name in ("summary.json", "protocol_lock.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            value = read_object(path)
        except Exception as exc:
            reasons.append(f"{name} is unreadable: {type(exc).__name__}")
            continue
        text = json.dumps(value, sort_keys=True).lower()
        if any(marker in text for marker in (
            "provider-free-static", "static_audit", "self-test", "self_test",
            "probe run", "probe_only", "replay", "mock run", "scheduled_after_freeze",
        )):
            reasons.append(f"{name} identifies static/self-test/mock/probe/replay evidence")
        classification = str(value.get("classification", "")).lower()
        if "infrastructure" in classification or "provider_failure" in classification:
            reasons.append(f"{name} classifies the run as infrastructure-invalid")
    return reasons


def validate_code_contract(run_dir: Path, path: Path, *, expected_digest: str | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    contract_path = inside(run_dir, path)
    contract = read_object(contract_path)
    if expected_digest is not None and contract.get('candidate_digest') != expected_digest:
        raise ValueError('Code contract is not bound to the frozen full-tree identity')
    if (contract.get('schema_version') != '0825-code-score-contract-v3'
            or contract.get('score_policy') != 'raw_score_final'
            or contract.get('contract_valid') is not True
            or contract.get('code_score_publishable') is not True
            or contract.get('errors') != []):
        raise ValueError('Code contract is not a valid native Create measurement')
    usage = contract.get("provider_usage")
    if not valid_judge_usage(usage):
        raise ValueError("Code judge provider usage is missing, non-positive, or inconsistent")
    if usage.get("logical_requests") != 1 or usage.get("completed_responses") != 1:
        raise ValueError("Code judge must contain exactly one logical request and one completed response")
    dimensions = contract.get("code_dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(CODE_WEIGHTS):
        raise ValueError("Code judge contract must contain exactly the eight fixed dimensions")
    total = 0
    for name, weight in CODE_WEIGHTS.items():
        item = dimensions.get(name)
        score = item.get("score") if isinstance(item, dict) else None
        evidence = item.get("evidence") if isinstance(item, dict) else None
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= weight:
            raise ValueError(f"Code dimension {name} must be an integer in 0..{weight}")
        if item.get('max') != weight:
            raise ValueError(f'Code dimension {name} has the wrong native maximum')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"Code dimension {name} lacks independent judge evidence")
        total += score
    if (type(contract.get('code_raw_score')) is not int or type(contract.get('code_score')) is not int
            or contract['code_raw_score'] != total or contract['code_score'] != total):
        raise ValueError('native Code score must equal the exact raw eight-dimension sum')
    # Native Create does not emit the old task's invented independence fields.
    # Verify its actual evaluator-only invocation and immutable input manifests.
    directory = contract_path.parent
    source_manifest = read_object(directory / 'source_manifest.json')
    requirements_manifest = read_object(directory / 'public_requirements_manifest.json')
    invocation = read_object(directory / 'code_judge_invocation.json')
    lifecycle = read_object(directory / 'code_judge_lifecycle.json')
    ledger = read_object(directory / 'code_transport_ledger.json')
    if (source_manifest.get('tree_digest') != expected_digest
            or sha256(directory / 'source_manifest.json') != contract.get('source_manifest_digest')
            or requirements_manifest.get('tree_digest') != contract.get('public_requirements_digest')
            or sha256(ROOT / 'evaluator/code_rubric.md') != contract.get('code_rubric_digest')):
        raise ValueError('Code immutable input manifest binding mismatch')
    line_counts = {item['path']: item['line_count'] for item in source_manifest.get('files', [])
                   if item.get('type') == 'text' and item.get('included_in_evidence_pack') is True}
    for name, item in dimensions.items():
        citations = item.get('verified_citations')
        if not isinstance(citations, list) or not citations:
            raise ValueError(f'Code dimension {name} lacks verified source citations')
        for citation in citations:
            if (not isinstance(citation, dict) or citation.get('path') not in line_counts
                    or type(citation.get('start_line')) is not int or type(citation.get('end_line')) is not int
                    or not 1 <= citation['start_line'] <= citation['end_line'] <= line_counts[citation['path']]):
                raise ValueError('Code source citation is not within the packed immutable source')
    if (invocation.get('schema_version') != 'agentswe-edit-code-judge-invocation-v1'
            or invocation.get('authoritative_judge') != '@@AGENTSWE_EDITING_CONTROL@@/code_eval.py'
            or any(invocation.get(key) != 'read-only' for key in ('candidate_mount', 'public_requirements_mount', 'rubric_mount'))
            or invocation.get('exit_code') != 0 or invocation.get('credential_values_recorded') is not False
            or (lifecycle.get('cleanup') or {}).get('complete') is not True
            or any(ledger.get(key) != usage.get(key) for key in ('logical_requests', 'transport_attempts',
                'completed_responses', 'input_tokens', 'output_tokens', 'total_tokens'))):
        raise ValueError('Code independent invocation/transport/cleanup evidence is invalid')
    judge = contract.get('judge') or {}
    if judge.get('model') != 'deepseek-flash' or judge.get('reasoning_effort') != 'max':
        raise ValueError('Code judge model/effort mismatch')
    return {**contract, 'source_contract': str(contract_path),
            'independence_evidence': str(directory / 'code_judge_invocation.json')}


def finalize(run_dir: Path, *, output: Path | None = None, code_contract: Path | None = None,
             result_judge_broker_endpoint: str | None = None, credential_file: Path | None = None,
             acceptance_cases: list[str] | None = None,
             scoring_dirs: dict[str, Path] | None = None,
             dispatch_zero_cases: bool = False) -> tuple[int, dict[str, Any]]:
    # Readiness reads the two axes from its own roots; with no override these
    # are exactly the formal paths this function has always written.
    scoring_dirs = dict(scoring_dirs or {})
    run_dir = run_dir.resolve()
    acceptance = acceptance_cases is not None
    cases = tuple(acceptance_cases) if acceptance else CASE_IDS
    if not cases or len(set(cases)) != len(cases) or any(case not in CASE_IDS for case in cases):
        raise ValueError("acceptance requires a unique nonempty hidden subset")
    output = (output or run_dir / ("acceptance_aggregation.json" if acceptance else "formal_aggregation.json")).resolve()
    reasons = run_marker_reasons(run_dir)
    session_path = run_dir / "builder_session_attestation.json"
    if acceptance and not session_path.is_file():
        session_path = run_dir / "pilot_builder_session_attestation.json"
    reuse = acceptance and (run_dir / "acceptance_source.json").is_file()
    if reuse:
        session_path = run_dir / "acceptance_source.json"
    freeze_path = run_dir / "lifecycle/freeze_manifest.json"
    hidden_attestation_path = run_dir / "lifecycle/hidden_after_freeze_attestation.json"
    hidden_run_path = run_dir / "hidden_after_freeze/hidden_run.json"
    if acceptance and not hidden_run_path.is_file():
        hidden_run_path = run_dir / "pilot_hidden_after_freeze/hidden_run.json"
    for label, path in (
        ("Builder session attestation", session_path),
        ("final Candidate freeze manifest", freeze_path),
        ("hidden-after-freeze attestation", hidden_attestation_path),
        ("hidden run", hidden_run_path),
    ):
        if not path.is_file():
            reasons.append(f"missing {label}: {path}")
    if reasons:
        result = rejection_payload(reasons)
        write_json(output, result)
        return 2, result

    try:
        session = read_object(session_path)
        freeze, frozen_candidate, frozen_digest = validate_freeze(freeze_path)
        hidden_attestation = read_object(hidden_attestation_path)
        hidden_run = read_object(hidden_run_path)
    except Exception as exc:
        result = rejection_payload([f"formal evidence is unreadable or invalid: {type(exc).__name__}: {exc}"])
        write_json(output, result)
        return 2, result

    accepted_rounds = session.get("accepted_rounds")
    if not isinstance(accepted_rounds, int):
        accepted_rounds = len(session.get("candidate_records", [])) if isinstance(session.get("candidate_records"), list) else 0
    if reuse:
        accepted_rounds = 0
        if (session.get("mode") != "acceptance_reuse" or session.get("candidate_digest") != frozen_digest
                or session.get("source_modified") is not False or session.get("builder_lifecycle_replayed") is not False):
            reasons.append("acceptance source must be an immutable explicitly non-Builder diagnostic copy")
    if not reuse and session.get("schema_version") not in {"agentswe-claude-builder-session-attestation/v1", *( ["agentswe-claude-pilot-builder-session-attestation/v1"] if acceptance else [])}:
        reasons.append("unsupported Builder attestation schema")
    if not reuse and (session.get("single_harbor_invocation") is not True or session.get("single_continuous_session") is not True):
        reasons.append("Builder was not one continuous same-session invocation")
    if not reuse and (accepted_rounds < 1 or accepted_rounds > 10):
        reasons.append("accepted Builder rounds must be between 1 and 10")
    if not reuse and (session.get("builder_model") != "deepseek-flash" or session.get("builder_reasoning_effort") != "max"):
        reasons.append("Builder model/reasoning lock mismatch")
    if not reuse and (session.get("builder_exit_code") != 0 or session.get("builder_timed_out") is True):
        reasons.append("Builder did not exit successfully")
    if not reuse and not isinstance(session.get("freeze"), dict):
        reasons.append("Builder attestation has no freeze")

    for number in range(1, accepted_rounds + 1):
        round_path = run_dir / f"lifecycle/round_{number:03d}.json"
        if not round_path.is_file():
            reasons.append(f"missing Candidate {number} public round evidence")
            continue
        try:
            record = read_object(round_path)
        except Exception as exc:
            reasons.append(f"Candidate {number} round unreadable: {type(exc).__name__}")
            continue
        dev = record.get("dev")
        if record.get("accepted") is not True or record.get("round_consumed") is not True:
            reasons.append(f"Candidate {number} was not an accepted consumed round")
        # single-dev-two-round-hidden-smoke-v1 runs one public case per round;
        # only the formal evaluation runs both. Read the profile the Builder
        # attested rather than assuming the formal shape.
        public_ids = (["dev_001"]
                      if session.get("readiness_profile") == "single-dev-two-round-hidden-smoke-v1"
                      else ["dev_001", "dev_002"])
        if not isinstance(dev, list) or [item.get("case_id") for item in dev if isinstance(item, dict)] != public_ids:
            reasons.append(f"Candidate {number} did not execute exactly both public dev cases")
        if number > 1 and not record.get("feedback"):
            reasons.append(f"Candidate {number} has no evaluator feedback binding")

    if freeze.get("source_submission") not in (None, accepted_rounds) and freeze.get("source_submission_id") not in {f"candidate_{accepted_rounds:03d}", f"candidate-{accepted_rounds:03d}"}:
        reasons.append("freeze is not sourced from the latest accepted Candidate")
    if freeze.get("hidden_case_inventory") != list(cases):
        reasons.append("freeze hidden inventory is not canonical test_001..test_006")
    if candidate_digest(frozen_candidate) != frozen_digest:
        reasons.append("frozen Candidate digest changed before finalization")

    summary = hidden_attestation.get("summary") if isinstance(hidden_attestation.get("summary"), dict) else {}
    ordering = hidden_attestation.get("ordering") if isinstance(hidden_attestation.get("ordering"), dict) else {}
    if hidden_attestation.get("schema_version") != "agentswe-claude-hidden-after-freeze-attestation/v1":
        reasons.append("unsupported hidden attestation schema")
    if summary.get("inventory_count") != len(cases):
        reasons.append("hidden attestation does not prove the selected inventory")
    if summary.get("case_spec_unavailable_count") != 0:
        reasons.append("hidden attestation contains failed or unavailable cases")
    if summary.get("scheduled_records_used_as_results") is not False:
        reasons.append("scheduled hidden records were used as results")
    if ordering.get("freeze_before_hidden") is not True or ordering.get("candidate_digest_stable_after_hidden") is not True:
        reasons.append("hidden execution ordering or frozen digest stability failed")
    for key in ("hidden_case_specs_not_mounted_in_candidate", "evaluator_source_not_mounted", "provider_credential_not_mounted"):
        if (hidden_attestation.get("isolation") or {}).get(key) is not True:
            reasons.append(f"hidden isolation gate is false: {key}")

    if hidden_run.get("schema_version") != "agentswe-claude-hidden-run/v1" or hidden_run.get("hidden_after_freeze") is not True:
        reasons.append("hidden run is not a real post-freeze Claude run")
    if hidden_run.get("case_inventory") != list(cases):
        reasons.append("hidden run inventory is not canonical test_001..test_006")
    if (hidden_run.get("source_records") or {}).get("scheduled_after_freeze_used_as_result") is not False:
        reasons.append("hidden run admits scheduled/replayed evidence")
    if hidden_run.get("candidate_digest") != frozen_digest or hidden_run.get("candidate_digest_after") != frozen_digest:
        reasons.append("hidden run is not digest-bound to the frozen Candidate")
    if inside(run_dir, str(hidden_run.get("freeze_manifest", ""))) != freeze_path.resolve():
        reasons.append("hidden run points to a different freeze manifest")

    records = hidden_run.get("cases")
    if not isinstance(records, list) or len(records) != len(cases):
        reasons.append("hidden run must contain exactly six case records")
        by_case: dict[str, Any] = {}
    else:
        by_case = {str(item.get("case_id")): item for item in records if isinstance(item, dict)}
        if list(by_case) != list(cases):
            reasons.append("hidden records are not in canonical test_001..test_006 order")

    attested_cases = hidden_attestation.get("cases")
    attested_by_case = {
        str(item.get("case_id")): item for item in attested_cases if isinstance(item, dict)
    } if isinstance(attested_cases, list) else {}
    case_payloads: dict[str, dict[str, Any]] = {}
    judge_inputs: dict[str, dict[str, Path]] = {}
    zero_contracts: dict[str, dict] = {}
    for case_id in cases:
        record = by_case.get(case_id)
        attested = attested_by_case.get(case_id)
        if not isinstance(record, dict) or not isinstance(attested, dict):
            reasons.append(f"{case_id}: missing run-local execution/attestation record")
            continue
        verdict, zero = execution_verdict(record, case_id, frozen_digest)
        if zero is not None and not dispatch_zero_cases:
            zero_contracts[case_id] = zero
            continue
        if verdict.get("classification") == "infrastructure_invalid":
            reasons.append(f"{case_id}: {verdict.get('reason')}")
            continue
        # `real_execution_evidence` is False precisely when the Candidate authored
        # no artifact, so under readiness -- which judges such a case on purpose --
        # the execution itself is what must be evidenced: an executed status and a
        # trajectory the product actually left behind.
        really_executed = (record.get("status") == "executed"
                           and (attested.get("real_execution_evidence") is True
                                or (dispatch_zero_cases and record.get("execution_attempted") is True)))
        if not really_executed:
            reasons.append(f"{case_id}: hidden case was not really executed")
        classification = str(record.get("classification", ""))
        evidence_classification = str(attested.get("evidence_classification", ""))
        if classification in INFRA_CLASSIFICATIONS or evidence_classification in INFRA_CLASSIFICATIONS:
            reasons.append(f"{case_id}: infrastructure-invalid hidden evidence")
        try:
            trajectory_path = inside(run_dir, str(record.get("trajectory_path", "")))
            absent_artifact = bool(dispatch_zero_cases) and not record.get("result_path")
            result_path = None if absent_artifact else inside(run_dir, str(record.get("result_path", "")))
        except ValueError as exc:
            reasons.append(f"{case_id}: {exc}")
            continue
        # The trajectory is the product's own record and is always required. The
        # final artifact is the Candidate's, and readiness judges its absence
        # through an explicit evaluator observation instead of refusing here.
        if not trajectory_path.is_file() or not (absent_artifact or result_path.is_file()):
            reasons.append(f"{case_id}: trajectory or final artifact is missing")
            continue
        if record.get("trajectory_sha256") != sha256(trajectory_path) or (
                not absent_artifact and record.get("result_sha256") != sha256(result_path)):
            reasons.append(f"{case_id}: evidence digest is not stable")
            continue
        try:
            trajectory = read_object(trajectory_path)
            artifact = None if absent_artifact else read_object(result_path)
        except Exception as exc:
            reasons.append(f"{case_id}: evidence JSON is invalid: {type(exc).__name__}")
            continue
        if trajectory.get("schema_version") != "agentswe-claude-policy-agent-case/v1" or trajectory.get("case_id") != case_id:
            reasons.append(f"{case_id}: trajectory schema/case binding is invalid")
        if trajectory.get("credential_seen_by_candidate") != "broker-only-placeholder":
            reasons.append(f"{case_id}: Candidate credential isolation is not attested")
        if trajectory.get("lower_entrypoint") != "candidate Claude policy hook + policy-ledger-inspect":
            reasons.append(f"{case_id}: target Claude product entry was not used")
        broker = trajectory.get("broker") if isinstance(trajectory.get("broker"), dict) else {}
        if broker.get("required") != {"model": MODEL, "reasoning_effort": REASONING_EFFORT}:
            reasons.append(f"{case_id}: lower model/reasoning lock mismatch")
        observed_delta = delta(broker.get("after"), broker.get("before"))
        if observed_delta["calls"] <= 0 or observed_delta["successful_calls"] <= 0:
            reasons.append(f"{case_id}: no successful real broker call")
        if any(observed_delta[key] != 0 for key in (
            "failures", "broker_failures", "provider_failures", "credential_failures", "protocol_failures",
        )):
            reasons.append(f"{case_id}: broker/provider/credential/protocol failure occurred")
        if absent_artifact:
            # Readiness judges the absence itself; the evaluator-written absence
            # observation is produced further down. Consistency of the record is
            # still enforced here so a real artifact can never be masked as absent.
            if record.get("artifact_present") is not False or record.get("result_sha256") is not None:
                reasons.append(f"{case_id}: absent-artifact record is inconsistent")
        else:
            if artifact.get("schema_version") != "agentswe-claude-policy-agent-result/v1" or artifact.get("case_id") != case_id:
                reasons.append(f"{case_id}: final artifact contract is invalid")
            if trajectory.get("answer") != artifact:
                reasons.append(f"{case_id}: scored answer differs from the authored final artifact")
            if trajectory.get("binding") != artifact.get("binding"):
                reasons.append(f"{case_id}: final artifact is not bound to the product trajectory")
            artifact_meta = trajectory.get("artifact") if isinstance(trajectory.get("artifact"), dict) else {}
            if artifact_meta.get("binding_verified") is not True or record.get("artifact_binding_valid") is not True:
                reasons.append(f"{case_id}: artifact binding was not independently verified")
        evidence_paths: dict[str, Path] = {}
        for label, path_key, digest_key in (
            ("task input", "task_input_path", "task_input_sha256"),
            ("task-local rubric", "task_local_rubric_path", "task_local_rubric_sha256"),
            ("native evidence", "native_evidence_path", "native_evidence_sha256"),
            ("oracle comparison", "oracle_comparison_path", "oracle_comparison_sha256"),
        ):
            try:
                evidence_path = inside(run_dir, str(record.get(path_key, "")))
            except ValueError as exc:
                reasons.append(f"{case_id}: {label} path invalid: {exc}")
                continue
            if not evidence_path.is_file() or record.get(digest_key) != sha256(evidence_path):
                reasons.append(f"{case_id}: {label} is missing or digest-mismatched")
                continue
            evidence_paths[path_key] = evidence_path
        if len(evidence_paths) == 4:
            try:
                oracle_comparison = read_object(evidence_paths["oracle_comparison_path"])
                native_evidence = read_object(evidence_paths["native_evidence_path"])
            except Exception as exc:
                reasons.append(f"{case_id}: run-local judge evidence is unreadable: {type(exc).__name__}")
            else:
                if oracle_comparison.get("schema_version") != "agentswe-claude-oracle-comparison/v1" or oracle_comparison.get("case_id") != case_id:
                    reasons.append(f"{case_id}: oracle comparison is not case-bound")
                if oracle_comparison.get("private_values_disclosed") is not False:
                    reasons.append(f"{case_id}: oracle comparison discloses private values")
                if native_evidence.get("schema_version") != "agentswe-claude-native-evidence/v1" or native_evidence.get("case_id") != case_id:
                    reasons.append(f"{case_id}: native evidence is not case-bound")
                if not absent_artifact and native_evidence.get("binding") != artifact.get("binding"):
                    reasons.append(f"{case_id}: native evidence binding differs from the authored artifact")
                judge_inputs[case_id] = evidence_paths
        case_payloads[case_id] = trajectory

    if reasons:
        result = rejection_payload(reasons)
        write_json(output, result)
        return 2, result

    if not result_judge_broker_endpoint:
        reasons.append("formal Result publication requires an evaluator-owned xhigh judge broker endpoint")
        result = rejection_payload(reasons)
        write_json(output, result)
        return 2, result
    score_dir = Path(scoring_dirs.get("result") or run_dir / "formal_scoring/result_axis")
    case_scores: dict[str, int] = {}
    score_records: dict[str, Any] = {}
    for case_id in cases:
        if case_id in zero_contracts:
            score_records[case_id] = zero_contracts[case_id]
            case_scores[case_id] = 0
            write_json(score_dir / case_id / "result_score_contract.json", zero_contracts[case_id])
            continue
        record = by_case[case_id]
        inputs = judge_inputs[case_id]
        task_input = inputs["task_input_path"]
        rubric = inputs["task_local_rubric_path"]
        native = inputs["native_evidence_path"]
        oracle = inputs["oracle_comparison_path"]
        judge_output = score_dir / case_id
        try:
            artifact_path = record.get("result_path")
            if not artifact_path:
                # Readiness still needs one real judgement, so the judge receives
                # an evaluator observation of the absence rather than a Candidate
                # artifact that does not exist. It is never presented as authored.
                artifact_path = run_dir / "readiness_judge_inputs/artifact_absence_observation.json"
                write_json(artifact_path, {"evidence_kind": "evaluator_observed_absence",
                                           "candidate_authored": False, "artifact_present": False,
                                           "case_id": case_id,
                                           "classification": record.get("classification")})
            contract = judge_evidence(case_id=case_id, artifact=inside(run_dir, str(artifact_path)),
                       trajectory=inside(run_dir, str(record["trajectory_path"])), task_input=task_input,
                       rubric=rubric, native=native, oracle=oracle, endpoint=result_judge_broker_endpoint, output=judge_output)
        except SemanticMeasurementUnavailable as exc:
            result = rejection_payload([f'{case_id}: Result judge returned an invalid measurement; see original contract'])
            result.update(mode='acceptance' if acceptance else 'formal', selected_cases=list(cases),
                acceptance_complete=False, acceptance_result_publishable=False,
                result_judge_contracts={**score_records, case_id: exc.contract},
                result_judge_failure={'case_id': case_id, 'contract_path': str(exc.contract_path),
                    'contract_sha256': sha256(exc.contract_path), 'exit_code': exc.exit_code},
                code_judge_invoked=False)
            write_json(output, result)
            return 2, result
        score_records[case_id] = contract
        _expected = 2 if contract.get("early_stop_resample") else 1
        if not (contract.get("contract_valid") is True and contract.get("result_score_publishable") is True and
                (contract.get("provider_usage") or {}).get("logical_requests") == _expected and
                (contract.get("provider_usage") or {}).get("completed_responses") == _expected and
                valid_judge_usage(contract.get("provider_usage"))):
            result = rejection_payload([f"{case_id}: shared semantic Result judge contract invalid"])
            result["result_judge_contracts"] = score_records
            write_json(output, result)
            return 2, result
        case_scores[case_id] = int(contract["result_score"])

    result_axis = {
        "score": round(sum(case_scores.values()) / len(cases), 4),
        "maximum": 100,
        "aggregation": "arithmetic mean of selected independent hidden cases",
        "case_scores": case_scores,
        "score_records": score_records,
    }
    code_axis: dict[str, Any] | str = "N/A"
    code_publishable = False
    # Code axis retired 2026-09-19 (Result-only): the Create Code judge is not dispatched;
    # completeness of acceptance/formal scoring depends on the Result axis alone.
    code_axis_skipped = True
    if code_axis_skipped:
        code_axis = {"id": "edit-code-axis-retired-2026-09-19", "evaluation_state": "skipped_by_policy", "reason": "Result-only evaluation; Code judge not dispatched"}
    elif code_contract is None and credential_file is not None and CODE_JUDGE.is_file():
        code_output = Path(scoring_dirs.get("code") or run_dir / "formal_scoring/code_axis")
        requirements, code_digest = prepare_code_inputs(run_dir, frozen_candidate, frozen_digest)
        code_process = subprocess.run([
            sys.executable, str(CODE_JUDGE), "--candidate-source", str(frozen_candidate),
            "--public-requirements", str(requirements), "--code-rubric", str(ROOT / "evaluator/code_rubric.md"),
            "--credential-file", str(credential_file), "--output-dir", str(code_output),
            "--expected-candidate-digest", code_digest, "--timeout", "2400",
        ], text=True, capture_output=True, check=False)
        generated = code_output / "code_score_contract.json"
        if generated.is_file():
            code_axis = read_object(generated)
            usage = code_axis.get("provider_usage")
            code_publishable = (code_axis.get("code_score_publishable") is True
                                and code_axis.get("candidate_digest") == code_digest
                                and code_tree_digest(frozen_candidate) == code_digest
                                and candidate_digest(frozen_candidate) == frozen_digest
                                and valid_judge_usage(usage)
                                and usage.get("logical_requests") == 1
                                and usage.get("completed_responses") == 1)
        else:
            reasons.append(f"Create Code judge did not produce a contract (exit {code_process.returncode})")
    if code_contract is not None:
        try:
            _, code_digest = prepare_code_inputs(run_dir, frozen_candidate, frozen_digest)
            code_axis = validate_code_contract(run_dir, code_contract, expected_digest=code_digest)
        except Exception as exc:
            result = rejection_payload([f"independent Code judge contract failed validation: {type(exc).__name__}: {exc}"])
            write_json(output, result)
            return 2, result
        write_json(run_dir / "formal_scoring/code_axis/code_score.json", code_axis)
        code_publishable = True

    result = {
        "schema_version": SCHEMA,
        "formal_result_publishable": not acceptance,
        "mode": "acceptance" if acceptance else "formal", "selected_cases": list(cases),
        "acceptance_result_publishable": acceptance, "acceptance_complete": acceptance and (code_publishable or code_axis_skipped),
        "code_score_publishable": code_publishable,
        "result_axis": result_axis,
        "code_axis": code_axis,
        "combined_score": None,
        "result_judge_contracts": score_records,
        "source_evidence": {
            "builder_session_attestation": str(session_path),
            "freeze_manifest": str(freeze_path),
            "hidden_attestation": str(hidden_attestation_path),
            "hidden_run": str(hidden_run_path),
        },
    }
    write_json(output, result)
    return (0 if (code_publishable or code_axis_skipped) else 2), result


def self_test() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    fixtures = {
        "partial": {"status": "lifecycle_execution_complete_unscored"},
        "static": {"mode": "provider-free-static-audit"},
        "probe": {"status": "explicit probe run"},
        "replay": {"status": "replay"},
        "infra": {"classification": "provider_failure"},
    }
    with tempfile.TemporaryDirectory(prefix="claude-formal-finalizer-self-test-") as raw:
        root = Path(raw)
        for name, summary in fixtures.items():
            run = root / name
            run.mkdir()
            write_json(run / "summary.json", summary)
            code, result = finalize(run)
            checks.append({
                "id": f"reject_{name}",
                "passed": code == 2 and result.get("result_axis") == "N/A" and result.get("code_axis") == "N/A",
            })
    return {
        "schema_version": "agentswe-claude-formal-finalizer-self-test/v1",
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--code-contract", type=Path, help="run-local independent eight-dimension judge contract")
    parser.add_argument("--result-judge-broker-endpoint", required=False)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--acceptance-cases", nargs="+")
    parser.add_argument("--self-test", action="store_true", help="provider-free rejection controls")
    args = parser.parse_args(argv)
    if args.self_test:
        value = self_test()
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0 if value["passed"] else 1
    if args.run_dir is None:
        parser.error("--run-dir is required unless --self-test is used")
    code, value = finalize(args.run_dir, output=args.output, code_contract=args.code_contract,
                           result_judge_broker_endpoint=args.result_judge_broker_endpoint,
                           credential_file=args.credential_file, acceptance_cases=args.acceptance_cases)
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
