"""One semantic Result scoring path for public dev and post-freeze hidden.

Task launchers retain responsibility for real product execution, task-specific
artifact validation and sanitizing the private oracle/trajectory. A scoring
directory is immutable by input identity and may make at most one invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from execution_contract import (candidate_zero_result_contract,
                                classify_candidate_execution, file_digest)

RESULT_JUDGE = Path(__file__).resolve().with_name("result_judge.py")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def validate_judge_contract(value, case_id):
    usage = value.get("provider_usage") or {}
    judge = value.get("judge") or {}
    return bool(
        value.get("case_id") == case_id and value.get("contract_valid") is True
        and value.get("result_score_publishable") is True
        and judge.get("model") == "deepseek-flash" and judge.get("reasoning_effort") == "max"
        and usage.get("logical_requests") == (2 if value.get("early_stop_resample") else 1)
        and usage.get("completed_responses") == (2 if value.get("early_stop_resample") else 1)
        and type(usage.get("transport_attempts")) is int and usage["transport_attempts"] >= 1
        and all(type(usage.get(k)) is int and usage[k] > 0 for k in ("input_tokens", "output_tokens", "total_tokens"))
        and usage["total_tokens"] >= usage["input_tokens"] + usage["output_tokens"]
        and type(value.get("result_score")) is int and 0 <= value["result_score"] <= 100)


def judge_execution_case(*, case_input, rubric, artifact, raw_trajectory,
                         native_evidence, private_oracle, execution_record,
                         candidate_digest, case_id, output, broker_endpoint,
                         score_cap_contract=None):
    """Return normalized validity and feedback; never invent semantic scores."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    contract_path = output / "result_score_contract.json"
    verdict = classify_candidate_execution(execution_record, case_id=case_id,
                                          candidate_digest=candidate_digest)
    def result(classification, reason, contract=None, cached=False):
        contract = contract or {}
        valid = classification in {"scoreable", "candidate_zero"}
        return {"classification": classification, "score": contract.get("result_score") if valid else None,
                "contract_valid": valid, "round_consumed": valid,
                "contract_path": str(contract_path) if contract_path.is_file() else None,
                "usage": contract.get("provider_usage"), "reason": reason, "cached": cached,
                "assessment": contract.get("assessment"),
                "major_errors": contract.get("major_errors"),
                "dimensions": contract.get("dimensions") or {
                    key: contract[key] for key in ("task_completion", "evidence_grounding", "recovery_and_safety")
                    if key in contract}}
    if verdict["classification"] not in {"scoreable", "candidate_zero"}:
        return result(verdict["classification"], verdict["reason"])
    paths = {"task_input": case_input, "rubric": rubric, "agent_artifact": artifact,
             "trajectory": raw_trajectory, "native_evidence": native_evidence,
             "oracle_summary": private_oracle}
    if score_cap_contract is not None:
        paths["score_cap_contract"] = score_cap_contract
    rubric_path = Path(rubric)
    dimension_candidates = [rubric_path.parent / "result_dimensions.json"]
    if rubric_path.parent.name == "agentloop":
        dimension_candidates.append(rubric_path.parent.parent / "evaluator" / "result_dimensions.json")
    dimension_path = next((p for p in dimension_candidates if p.is_file()), None)
    if dimension_path is not None:
        paths["rubric_dimensions"] = dimension_path
    try:
        if verdict["classification"] == "scoreable":
            missing = [name for name, p in paths.items() if p is None or not Path(p).is_file()]
            if missing:
                return result("unresolved", "missing semantic judge inputs: " + ", ".join(missing))
            if file_digest(artifact) != execution_record["artifact_validation"]["sha256"]:
                return result("unresolved", "artifact changed after task-specific provenance validation")
            if not broker_endpoint:
                return result("infrastructure_invalid", "evaluator-owned Result broker is unavailable")
        manifest = {k: {"path": str(Path(p).resolve()), "sha256": file_digest(p)}
                    for k, p in paths.items() if p is not None and Path(p).is_file()}
        identity = {"case_id": case_id, "candidate_digest": candidate_digest,
                    "execution_record": execution_record, "inputs": manifest,
                    "judge_source_digest": file_digest(RESULT_JUDGE),
                    "model": "deepseek-flash", "effort": "max"}
        identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        intent_path = output / "scoring_intent.json"
        if intent_path.exists():
            intent = json.loads(intent_path.read_text())
            if intent.get("identity_sha256") != identity_hash:
                return result("unresolved", "scoring directory already belongs to different immutable inputs")
            if contract_path.is_file():
                existing = json.loads(contract_path.read_text())
                if verdict["classification"] == "candidate_zero":
                    expected = candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=candidate_digest)
                    if existing == expected:
                        return result("candidate_zero", verdict["reason"], existing, True)
                elif validate_judge_contract(existing, case_id):
                    return result("scoreable", "reused completed logical judge response", existing, True)
            return result("unresolved", "prior scoring attempt requires recovery; no completed response will be resampled")
        if contract_path.exists():
            return result("unresolved", "unbound preexisting judge contract cannot be adopted")
        try:
            with intent_path.open("x") as handle:
                json.dump({"identity_sha256": identity_hash, "identity": identity}, handle, indent=2)
        except FileExistsError:
            return result("unresolved", "another evaluator owns this scoring attempt")
        if verdict["classification"] == "candidate_zero":
            contract = candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=candidate_digest)
            write_json(contract_path, contract)
            return result("candidate_zero", verdict["reason"], contract)
        command = [sys.executable, str(RESULT_JUDGE), "--case-id", case_id]
        for name, flag in (("task_input", "--task-input"), ("rubric", "--rubric"),
                           ("agent_artifact", "--agent-artifact"), ("trajectory", "--trajectory"),
                           ("native_evidence", "--native-evidence"), ("oracle_summary", "--oracle-summary")):
            command.extend([flag, str(Path(paths[name]).resolve())])
        command.extend(["--broker-endpoint", broker_endpoint, "--output-dir", str(output)])
        if score_cap_contract is not None:
            command.extend(["--score-cap-contract", str(Path(score_cap_contract).resolve())])
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if not contract_path.is_file():
            return result("infrastructure_invalid", "shared Result judge produced no contract")
        contract = json.loads(contract_path.read_text())
        if completed.returncode == 0 and validate_judge_contract(contract, case_id):
            return result("scoreable", "independent semantic Result judge completed", contract)
        state = contract.get("evaluation_state")
        return result("infrastructure_invalid" if state == "infrastructure_error" else "unresolved",
                      "shared Result judge did not produce a publishable semantic contract", contract)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return result("infrastructure_invalid", "evaluator preparation failed: " + type(exc).__name__)
