"""Independent OpenHands Result/Code readiness smoke.

The caller must give this smoke a fresh Result broker whose ledger contains no
public-round traffic. Outputs are readiness-only and cannot publish scores.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

PROFILE = "single-dev-two-round-hidden-smoke-v1"
SHARED_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
RESULT_JUDGE = SHARED_ROOT / "result_judge.py"
CODE_JUDGE = SHARED_ROOT / "code_judge_runner.py"
CODE_VALIDATOR = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
CODE_AXIS_SKIP_POLICY = {"id": "edit-code-axis-retired-2026-09-19", "evaluation_state": "skipped_by_policy", "reason": "Result-only evaluation; Code judge not dispatched"}


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def ref(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def judge_payload(model_path):
    """The Result judge's own parsed verdict, not a second parse of its raw answer.

    D18 (2026-09-21) lets the shared judge repair a malformed answer ENVELOPE --
    unclosed or surplus closing delimiters, a non-JSON trailer, unknown top-level
    keys -- publish a valid contract and record the repair as `envelope_repair`,
    while `model_response.json` keeps the VERBATIM bytes the model sent.  Parsing
    those bytes a second time here raised JSONDecodeError and killed a whole
    readiness run for a case the judge had already scored (aider 0921-v4-002,
    result_score 99, DSML tool-call markup after a complete object).

    No repair recorded -> nothing changes: the verbatim answer is parsed exactly
    as before.  A repair recorded -> the judge's own validated verdict
    (`result_eval_result.json`) is used, but only after the contract proves it is
    publishable and its `model_response_digest` still binds the verbatim bytes on
    disk, which is the same file and the same digest the judge itself bound.  An
    invalid contract stays invalid: the original decode error is re-raised and the
    caller fails exactly as it does today.  `model_response.json` is never
    rewritten and stays the raw evidence `raw_model_output` digests.
    """
    model_path = Path(model_path)
    raw = model_path.read_bytes()
    verdict, decode_error = None, None
    try:
        verdict = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        decode_error = exc

    def verbatim():
        if decode_error is not None:
            raise decode_error
        if not isinstance(verdict, dict):
            raise ValueError("judge answer must be one JSON object: %s" % model_path)
        return verdict

    try:
        contract = json.loads(
            (model_path.parent / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return verbatim()
    repair = contract.get("envelope_repair") if isinstance(contract, dict) else None
    if not isinstance(repair, dict) or repair.get("applied") is not True:
        return verbatim()
    # The judge writes the answer plus exactly one newline and digests the answer.
    digest = hashlib.sha256(raw[:-1]).hexdigest() if raw.endswith(b"\n") else None
    if (contract.get("contract_valid") is not True
            or contract.get("result_score_publishable") is not True
            or contract.get("evaluation_state") != "scoreable"
            or contract.get("errors") != []
            or digest is None
            or contract.get("model_response_digest") != digest
            or repair.get("original_sha256") != digest):
        return verbatim()
    repaired = json.loads(
        (model_path.parent / "result_eval_result.json").read_text(encoding="utf-8"))
    if (not isinstance(repaired, dict)
            or repaired.get("case_id") != contract.get("case_id")
            or repaired.get("result_state") != contract.get("result_state")
            or repaired.get("result_score") != contract.get("result_score")):
        raise ValueError("repaired judge verdict disagrees with its contract: %s" % model_path)
    # `arithmetic_repair` is the judge's own repair record, not part of the answer
    # envelope the shared bundle validator re-checks; the contract keeps it.
    return {key: value for key, value in repaired.items() if key != "arithmetic_repair"}


def read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("OpenHands readiness evidence must be an object")
    return value


def broker_stats(endpoint: str) -> dict:
    request = urllib.request.Request(
        endpoint.rsplit("/v1/responses", 1)[0] + "/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def dispatch_pair(result_command, code_command, output: Path, *, provider_dispatch_authorized=False,
                  runner=None) -> dict[str, int]:
    if provider_dispatch_authorized is not True:
        raise RuntimeError("readiness provider dispatch requires explicit caller authorization")
    output.mkdir(parents=True, exist_ok=False)
    runner = runner or subprocess.run
    write(output / "dispatch_intent.json", {
        "profile": PROFILE, "evaluation_mode": "readiness_smoke",
        "formal_result_publishable": False, "code_score_publishable": False,
        "commands": {"result": result_command, "code": code_command},
    })
    exits = {}
    for role, command in [(r, c) for r, c in (("result", result_command), ("code", code_command)) if c is not None]:
        completed = runner(command, capture_output=True, text=True, check=False)
        (output / f"{role}.stdout.log").write_text(completed.stdout)
        (output / f"{role}.stderr.log").write_text(completed.stderr)
        exits[role] = completed.returncode
    write(output / "dispatch_terminal.json", exits)
    return exits


def _result_dimension_maxima(task_root, rubric_relative_path):
    """Exactly the maxima readiness_judge_validation recomputes from this task's rubric."""
    if str(SHARED_ROOT) not in sys.path:
        sys.path.insert(0, str(SHARED_ROOT))  # result_judge imports responses_stream from the control plane
    spec = importlib.util.spec_from_file_location("readiness_result_judge", str(RESULT_JUDGE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_dimensions(module.rubric_dimensions_path(Path(task_root) / rubric_relative_path))


def export_judge_observations(run_dir: Path, freeze_path: Path, output: Path,
                              broker_before: dict, broker_after: dict) -> dict:
    before_ids = {row.get("request_id") for row in broker_before.get("attempts", [])}
    result_attempts = [row for row in broker_after.get("attempts", [])
                       if row.get("request_id") not in before_ids]
    if len(result_attempts) != 1 or not result_attempts[0].get("request_id"):
        raise ValueError("readiness Result smoke lacks one fresh request identity")
    result_id = result_attempts[0]["request_id"]
    code_id = None  # Code axis retired 2026-09-19 (Result-only)
    freeze_sha = ref(freeze_path)["sha256"]
    observations = {}
    models = {"result": output / "result/model_response.json",
              "code": output / "code/code_model_response.json"}
    validators = {"result": RESULT_JUDGE, "code": CODE_VALIDATOR}
    identities = {"result": "result:" + result_id, "code": None}
    requests = {"result": result_id, "code": code_id}
    for role in ("result",):
        source_input = output.parent / f"readiness_judge_inputs/{role}_validation_input.json"
        model = models[role]
        if not source_input.is_file() or not model.is_file():
            raise ValueError("readiness judge input/output bytes are missing: " + role)
        output_path = output / f"{role}_raw_output.json"
        write(output_path, {"role": role, "judge_session_id": identities[role],
                            "raw_model_output": ref(model), "payload": judge_payload(model)})
        observation = output / f"{role}_observation.json"
        write(observation, {
            "owner": "evaluator", "run_id": run_dir.name, "role": role,
            "judge_session_id": identities[role], "request_id": requests[role],
            "model": "deepseek-flash", "effort": "max", "state": "terminal",
            "formal": False, "freeze_sha256": freeze_sha,
            "input": ref(source_input), "output": ref(output_path),
            "validator_source_sha256": ref(validators[role])["sha256"],
        })
        observations[role] = ref(observation)
    # Code axis retired 2026-09-19 (Result-only): explicit evaluator-written skip observation.
    write(output / "code_observation.json", {"owner": "evaluator", "run_id": run_dir.name, "role": "code",
        "state": "skipped_by_policy", "formal": False, "judge_session_id": None, "request_id": None,
        "freeze_sha256": freeze_sha, "policy": CODE_AXIS_SKIP_POLICY})
    write(output / "judge_observations.json", observations)
    return observations


def accepted_evidence_paths(run_dir: Path, frozen: Path) -> list[str]:
    """Paths the accepted solution.patch touches, as the Code judge's evidence.

    Without this the pack is the whole repository, which exceeds the judge's
    text budget on this product. The delivery is identified by the accepted
    record and then bound by its own report before a byte of it is read.
    """
    record = json.loads((run_dir / "lifecycle/dev_lifecycle.json").read_bytes())["records"][-1]
    attempt = record["build"]["materialization"]["attempt"]
    delivery = run_dir / "deliveries" / ("attempt_%03d" % attempt)
    report = json.loads((delivery / "run_report.json").read_bytes())
    if (report.get("submission_number") != record["submission"]
            or report.get("builder_session_id") != record["builder_session_id"]):
        raise ValueError("accepted delivery does not match the accepted record")
    text = (delivery / "solution.patch").read_text(errors="replace")
    touched = set()
    for match in re.finditer(r"^\+\+\+ b/(\S+)", text, re.M):
        touched.add(match.group(1))
    for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)", text, re.M):
        touched.add(match.group(2))
    touched.discard("/dev/null")
    present = sorted(path for path in touched if (frozen / path).is_file())
    if not present:
        raise ValueError("accepted solution.patch touches nothing in the frozen candidate")
    return present


def run(*, task_root: Path, run_dir: Path, hidden: list[dict], credential: Path,
        result_endpoint: str, provider_dispatch_authorized=False) -> dict:
    task_root, run_dir = Path(task_root), Path(run_dir)
    freeze_path = run_dir / "lifecycle/freeze_manifest.json"
    freeze = read(freeze_path)
    if freeze.get("readiness_profile") != PROFILE or freeze.get("source_submission") != 2:
        raise ValueError("readiness smoke requires an exact two-round readiness freeze")
    if [row.get("case_id") for row in hidden] != ["test_001"]:
        raise ValueError("readiness hidden smoke must contain exactly test_001")
    record_path = run_dir / "lifecycle/hidden/test_001/case_result.json"
    record = read(record_path)
    if record.get("classification") in {
        "provider_failure", "broker_failure", "lower_agent_infrastructure_failure",
    }:
        raise ValueError("infrastructure-invalid hidden execution cannot enter judge smoke")

    output = run_dir / "readiness_scoring"
    preparation = run_dir / "readiness_judge_inputs"
    preparation.mkdir(exist_ok=False)
    sys.path.insert(0, str(task_root / "evaluator"))
    from case_evidence import prepare_case_evidence
    from semantic_finalize import stage_requirements, tree_digest, validate_code_contract, validate_result_contract

    frozen = run_dir / "lifecycle/frozen_candidate"
    candidate_digest = tree_digest(frozen)
    evidence = prepare_case_evidence(
        case_id="test_001", record_path=record_path, candidate=frozen,
        candidate_digest=candidate_digest, output=preparation / "result",
    )
    artifact = evidence.get("artifact")
    # D14 absence path.  An absent artifact is a Candidate outcome, not an
    # orchestration defect, whenever the evaluator actually bound the run: the
    # trajectory is present and the case record carries a FATAL CANDIDATE
    # attribution that the evaluator itself observed.  Score it zero here rather
    # than failing the pilot; there is no artifact for the Result judge to read.
    attribution = record.get("failure_attribution") or {}
    candidate_fatal = (
        record.get("classification") == "candidate_product_failure"
        and attribution.get("party") == "candidate"
        and attribution.get("fatal") is True
        and attribution.get("infrastructure_invalid") is not True
    )
    if artifact is None and candidate_fatal and evidence.get("raw_trajectory") is not None:
        summary = {
            "profile": PROFILE, "evaluation_mode": "readiness_smoke", "score_threshold": None,
            "formal_result_publishable": False, "code_score_publishable": False,
            "formal_complete": False, "readiness_judges_complete": True,
            "errors": {"result": None, "code": None}, "exits": {},
            "candidate_zero_without_artifact": {
                "schema_version": "agentswe-openhands-readiness-absence/v1",
                "case_id": "test_001", "result_score": 0, "result_state": "candidate_zero",
                "classification": record.get("classification"),
                "failure_attribution": attribution,
                "artifact_present": False,
                "trajectory": str(evidence["raw_trajectory"]),
                "native_evidence": str(evidence["native_evidence"]),
                "oracle_summary": str(evidence["private_oracle"]),
                "judge_dispatched": False,
                "reason": "Candidate produced no model-authored artifact; the run is bound and valid, so the absence is scored zero rather than booked as an orchestration failure.",
            },
        }
        write(run_dir / "readiness_judge_smoke.json", summary)
        return summary
    # 0921: an evaluator budget truncation is not a Candidate absence and must not be
    # scored zero by the D14 path above; readiness still fails closed, but it now says
    # which of the evaluator's own guards ended the case instead of "lacks artifact".
    if artifact is None and record.get("classification") == "evaluator_budget_truncation":
        raise ValueError("readiness hidden execution was cut by the evaluator's own case-budget guard, "
                         "not by the Candidate: " + str(attribution.get("reason")))
    if artifact is None or evidence.get("raw_trajectory") is None:
        raise ValueError("readiness hidden execution lacks bound model artifact/trajectory")
    requirements = stage_requirements(task_root, preparation)
    # The case input and rubric are task-owned inputs, not run evidence. The
    # bundle may only carry bytes from inside the run, so they are staged here
    # and both the judge and the recorded reference use the copy.
    staged_task_input = preparation / "case_input.md"
    staged_rubric = preparation / "case_rubric.md"
    shutil.copyfile(Path(evidence["case_input"]), staged_task_input)
    shutil.copyfile(Path(evidence["rubric"]), staged_rubric)
    # The shared judge discovers dimension maxima next to the rubric it is given; dev
    # rounds judge with evaluator/result_dimensions.json beside evaluator/result_rubric.md,
    # so the staged copy carries the same schema (else the smoke silently judges 50/30/20).
    shutil.copyfile(task_root / "evaluator/result_dimensions.json", preparation / "result_dimensions.json")
    result_input = preparation / "result_validation_input.json"
    code_input = preparation / "code_validation_input.json"
    write(result_input, {
        "role": "result", "freeze_sha256": ref(freeze_path)["sha256"],
        # The shared validator binds the judge source and recomputes the rubric
        # dimensions from this task's own rubric; both belong in the input artifact.
        "validator_source_sha256": ref(RESULT_JUDGE)["sha256"], "rubric_relative_path": "evaluator/result_rubric.md",
        "dimension_maxima": _result_dimension_maxima(task_root, "evaluator/result_rubric.md"),
        "task_input": ref(staged_task_input), "artifact": ref(Path(artifact)),
        "trajectory": ref(Path(evidence["raw_trajectory"])),
        "native_evidence": ref(Path(evidence["native_evidence"])),
        "oracle_summary": ref(Path(evidence["private_oracle"])),
    })
    write(code_input, {"role": "code", "freeze_sha256": ref(freeze_path)["sha256"],
                       "candidate_digest": candidate_digest})
    result_command = [
        sys.executable, str(RESULT_JUDGE), "--case-id", "test_001",
        "--task-input", str(staged_task_input), "--rubric", str(staged_rubric),
        "--agent-artifact", str(artifact), "--trajectory", str(evidence["raw_trajectory"]),
        "--native-evidence", str(evidence["native_evidence"]),
        "--oracle-summary", str(evidence["private_oracle"]),
        "--broker-endpoint", result_endpoint, "--max-transport-attempts", "1",
        "--output-dir", str(output / "result"), "--timeout", "2400",
    ]
    evidence_paths = accepted_evidence_paths(run_dir, frozen)
    write(preparation / "code_evidence_scope.json", {
        "schema_version": "agentswe-openhands-code-evidence-scope/v1",
        "source": "accepted solution.patch", "paths": evidence_paths})
    code_command = None  # Code axis retired 2026-09-19 (Result-only)
    broker_before = broker_stats(result_endpoint)
    exits = dispatch_pair(result_command, code_command, output,
                          provider_dispatch_authorized=provider_dispatch_authorized)
    broker_after = broker_stats(result_endpoint)
    result, result_error = validate_result_contract(output / "result/result_score_contract.json", "test_001")
    code, code_error = None, None  # Code axis retired 2026-09-19
    complete = exits == {"result": 0} and result is not None
    summary = {
        "profile": PROFILE, "evaluation_mode": "readiness_smoke", "score_threshold": None,
        "formal_result_publishable": False, "code_score_publishable": False,
        "formal_complete": False, "readiness_judges_complete": complete,
        "errors": {"result": result_error, "code": code_error}, "exits": exits,
    }
    if complete:
        summary["observations"] = export_judge_observations(
            run_dir, freeze_path, output, broker_before, broker_after,
        )
    write(run_dir / "readiness_judge_smoke.json", summary)
    return summary
