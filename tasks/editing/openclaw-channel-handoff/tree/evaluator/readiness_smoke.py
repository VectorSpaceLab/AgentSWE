"""Independent OpenClaw Result/Code readiness smoke.

The caller must give this smoke a fresh Result broker whose ledger contains no
public-round traffic. Outputs are readiness-only and cannot publish scores.

OpenClaw shares `semantic_finalize` with OpenHands, so the case evidence is
assembled by that module's own helpers rather than re-derived here. Two things
differ from the formal path it replaces: the single hidden case lives under
`pilot_hidden/`, not `hidden/`, and the Code evidence is focused on the accepted
delivery -- the frozen tree is ~382 MB and cannot be packed whole.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import urllib.request

PROFILE = "single-dev-two-round-hidden-smoke-v1"
SHARED_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
RESULT_JUDGE = SHARED_ROOT / "result_judge.py"
CODE_JUDGE = SHARED_ROOT / "code_judge_runner.py"
CODE_VALIDATOR = SHARED_ROOT / "code_eval.py"
CODE_AXIS_SKIP_POLICY = {"id": "edit-code-axis-retired-2026-09-19", "evaluation_state": "skipped_by_policy", "reason": "Result-only evaluation; Code judge not dispatched"}
CASE = "test_001"


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
        raise ValueError("OpenClaw readiness evidence must be an object")
    return value


def broker_stats(endpoint: str) -> dict:
    request = urllib.request.Request(
        endpoint.rsplit("/v1/responses", 1)[0] + "/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def accepted_evidence_paths(run_dir: Path, frozen: Path) -> list[str]:
    """Paths the accepted solution.patch touches, as the Code judge's evidence.

    The frozen OpenClaw tree is hundreds of megabytes, so packing it whole
    exceeds the judge's text budget outright. The accepted delivery is named by
    the last row of the submission ledger and bound by its own digest before a
    byte of it is read.
    """
    submissions = json.loads((run_dir / "builder_submissions.json").read_bytes())
    if not submissions:
        raise ValueError("readiness Code evidence requires an accepted submission")
    latest = submissions[-1]
    snapshot = Path(latest["delivery_snapshot"])
    names = ("solution.patch", "edit_report.json", "run_report.json")
    if sorted(path.name for path in snapshot.iterdir()) != sorted(names):
        raise ValueError("accepted delivery is not exactly the three contract files")
    text = (snapshot / "solution.patch").read_text(errors="replace")
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


def dispatch_pair(result_command, code_command, output: Path, runner=None) -> dict[str, int]:
    """One dispatch per role, exclusive run namespace, no retry or score threshold."""
    output.mkdir(parents=True, exist_ok=False)
    runner = runner or subprocess.run
    write(output / "dispatch_intent.json", {
        "profile": PROFILE, "evaluation_mode": "readiness_smoke",
        "formal_result_publishable": False,
        "commands": {"result": result_command, "code": code_command}})
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
        source_input = run_dir / "readiness_judge_inputs" / f"{role}_validation_input.json"
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
            "request_identity_kind": "broker_request_id" if role == "result" else "provider_response_id",
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


def run(*, task_root, run_dir, credential, result_endpoint, judge_timeout=2400):
    task_root, run_dir = Path(task_root), Path(run_dir)
    sys.path.insert(0, str(task_root / "evaluator"))
    from semantic_finalize import (artifact_for, evidence_files, inside, is_infrastructure,
                                   stage_requirements, task_rubric, tree_digest,
                                   validate_code_contract, validate_result_contract)

    freeze_path = run_dir / "lifecycle/freeze_manifest.json"
    freeze = read(freeze_path)
    if freeze.get("readiness_profile") != PROFILE:
        raise ValueError("readiness smoke requires an explicitly bound readiness freeze")
    frozen = Path(str(freeze.get("frozen_candidate_path", run_dir / "lifecycle/frozen_candidate")))
    expected = str(freeze.get("candidate_digest", ""))
    if not frozen.is_dir() or not inside(frozen, run_dir) or not expected:
        raise ValueError("frozen Candidate is missing or outside the run")
    if tree_digest(frozen) != expected:
        raise ValueError("frozen Candidate digest changed before judge smoke")

    # The readiness profile runs one hidden case through the pilot path, so its
    # attestation sits under pilot_hidden/. The suite index must agree with the
    # case's own attestation, which is the check the formal layout makes too.
    record_path = run_dir / "pilot_hidden" / CASE / "hidden_case_attestation.json"
    suite_path = run_dir / "pilot_hidden/hidden_result.json"
    record = read(record_path)
    suite = read(suite_path)
    if suite.get(CASE) != record:
        raise ValueError("suite index differs from the original case attestation")
    if record.get("case_id") != CASE:
        raise ValueError("native record case mismatch")
    if is_infrastructure(record):
        raise ValueError("infrastructure-invalid hidden execution cannot enter judge smoke")

    output = run_dir / "readiness_scoring"
    if output.exists():
        raise RuntimeError("readiness judge output already exists; no replay")
    preparation = run_dir / "readiness_judge_inputs"
    preparation.mkdir(exist_ok=False)

    try:
        artifact = artifact_for("openclaw", run_dir, CASE, record_path, record)
    except ValueError as exc:
        # Only an absent artifact is substituted. An artifact that escapes the
        # run, belongs to another case, was synthesized by the evaluator or
        # disagrees with the captured one still fails here.
        if "missing" not in str(exc):
            raise
        artifact = preparation / "artifact_absence_observation.json"
        write(artifact, {"evidence_kind": "evaluator_observed_absence",
                         "candidate_authored": False, "artifact_present": False,
                         "case_id": CASE, "classification": record.get("classification"),
                         "classification_reason": record.get("classification_reason"),
                         "execution_record": ref(Path(record["evaluator_run_report"]))})
    trajectory, native, oracle = evidence_files(
        task_root, run_dir, "openclaw", CASE, record_path, record, artifact)
    rubric = task_rubric(task_root, run_dir, CASE)
    # Same evidence-bound task-local ceilings the formal path issues, so a
    # readiness smoke exercises the identical judge contract.
    cap_contract, cap_error = None, None
    try:
        from result_score_caps import enabled as caps_enabled, write_contract
        if caps_enabled():
            cap_contract = write_contract(preparation / "score_cap_contract.json", CASE, record,
                                          rubric=rubric, native_evidence=native, oracle_summary=oracle)
    except Exception as exc:
        cap_contract, cap_error = None, f"{type(exc).__name__}: {exc}"
    requirements = stage_requirements(task_root, preparation)
    task_input_source = task_root / "test_cases" / CASE / "input.md"
    if not task_input_source.is_file():
        raise ValueError("hidden case input is missing")
    # Task-owned input staged into the evaluator-owned run: the bundle must be
    # self-contained, so judge inputs never reference the live task tree.
    task_input = preparation / "task_input.md"
    task_input.write_bytes(task_input_source.read_bytes())
    evidence_paths = accepted_evidence_paths(run_dir, frozen)
    write(preparation / "code_evidence_scope.json", {
        "schema_version": "agentswe-openclaw-code-evidence-scope/v1",
        "source": "accepted solution.patch", "paths": evidence_paths})
    write(preparation / "result_validation_input.json", {
        "role": "result", "freeze_sha256": ref(freeze_path)["sha256"],
        # The shared validator binds the judge source and recomputes the rubric
        # dimensions from this task's own rubric; both belong in the input artifact.
        "validator_source_sha256": ref(RESULT_JUDGE)["sha256"], "rubric_relative_path": "evaluator/result_rubric.md",
        "dimension_maxima": _result_dimension_maxima(task_root, "evaluator/result_rubric.md"),
        "task_input": ref(task_input), "rubric": ref(rubric), "artifact": ref(artifact),
        "trajectory": ref(trajectory), "native_evidence": ref(native),
        "oracle_summary": ref(oracle),
        "score_cap_contract": ref(cap_contract) if cap_contract is not None else None,
        "score_cap_error": cap_error})
    write(preparation / "code_validation_input.json", {
        "role": "code", "freeze_sha256": ref(freeze_path)["sha256"],
        "candidate_digest": expected, "evidence_paths": evidence_paths})

    result_command = [
        sys.executable, str(RESULT_JUDGE), "--case-id", CASE,
        "--task-input", str(task_input), "--rubric", str(rubric),
        "--agent-artifact", str(artifact), "--trajectory", str(trajectory),
        "--native-evidence", str(native), "--oracle-summary", str(oracle),
        "--broker-endpoint", result_endpoint, "--broker-placeholder", "broker-only-placeholder",
        "--max-transport-attempts", "1",
        "--output-dir", str(output / "result"), "--timeout", str(judge_timeout),
    ]
    if cap_contract is not None:
        result_command += ["--score-cap-contract", str(cap_contract)]
    code_command = None  # Code axis retired 2026-09-19 (Result-only)

    broker_before = broker_stats(result_endpoint)
    exits = dispatch_pair(result_command, code_command, output)
    broker_after = broker_stats(result_endpoint)
    write(output / "result_broker_attestation.json",
          {"before": broker_before, "after": broker_after})

    result_path = output / "result/result_score_contract.json"
    code_path = output / "code/code_score_contract.json"
    result, result_error = validate_result_contract(result_path, CASE)
    code, code_error = None, None  # Code axis retired 2026-09-19
    errors = {"result": [result_error] if result_error else [],
              "code": [code_error] if code_error else []}
    usage = (result or {}).get("provider_usage", {})
    if usage.get("logical_requests") != 1 or usage.get("completed_responses") != 1:
        errors["result"].append("Result smoke did not complete exactly one logical request")
    summary = {
        "profile": PROFILE, "evaluation_mode": "readiness_smoke", "score_threshold": None,
        "formal_result_publishable": False, "formal_complete": False,
        "code_score_publishable": False,
        "readiness_judges_complete": exits == {"result": 0}
                                     and not errors["result"],
        "result_contract": ref(result_path) if result_path.exists() else None,
        "code_contract": ref(code_path) if code_path.exists() else None,
        "errors": errors, "exits": exits,
    }
    if summary["readiness_judges_complete"]:
        summary["observations"] = export_judge_observations(
            run_dir, freeze_path, output, broker_before, broker_after)
    write(run_dir / "readiness_judge_smoke.json", summary)
    return summary
