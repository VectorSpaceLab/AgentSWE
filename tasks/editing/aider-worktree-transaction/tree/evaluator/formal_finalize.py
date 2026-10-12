#!/usr/bin/env python3
"""Finalize Aider's independent semantic Result and Code axes.

Deterministic code in this module only checks provenance, classifies
infrastructure, and prepares bounded evidence files.  Every scoreable hidden
case is sent once to the shared semantic Result judge; no local scorer can
publish a Result score.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluator.code_inputs import prepare_code_inputs
RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
# The evaluator-owned wrapper delegates the independent Code axis to the
# unchanged Create Code judge at this fixed path.
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
CASE_IDS = tuple(f"test_{index:03d}" for index in range(1, 7))
INFRA = {"infrastructure-invalid", "broker_infrastructure_error", "launcher_infrastructure_error",
         "provider_failure", "broker_infrastructure_failure", "provider_infrastructure_failure",
         "credential_infrastructure_failure", "mount_infrastructure_failure", "evaluator_infrastructure_failure"}


# --- judge resample (2026-09-20) ---------------------------------------------
# A shared Result judge that completed one paid-for response and then refused to
# publish it is an evaluator-side SAMPLING fault: the apparatus is healthy, the
# sample is not.  Booking it as infrastructure loses the Candidate's public
# round -- and on the ai-scientist tree it latched a terminal that ended the
# whole formal run (0905-edit-codex-xhigh-0919-fw-001-ai-scientist, 03:04 CST:
# deepseek-flash emitted a complete 100/100 verdict missing exactly one `}`,
# result_judge.close_unclosed_json appended it at the END of the text instead of
# at the container the model left open, and the misnested parse produced 31
# validation errors).  Across the 121 result contracts on this host 8 of the 65
# real judge calls are `model_output_invalid` -- about 12% -- so this is a
# routine event, not a freak one.
#
# The hidden axis already recovers by setting the refused attempt aside and
# judging again (see <run>/formal_scoring/result_axis/<case>.attempt-001-invalid
# on this host).  This gives the public round the same remedy, automatically and
# exactly once.  Nothing measured is repeated: the Candidate product is not
# re-executed, no lower-agent call is re-issued, the immutable evidence inputs
# are reused byte for byte, and the refused attempt is renamed rather than
# deleted so both verdicts stay auditable.  The control plane's
# one-response-per-immutable-directory contract (execution_scoring.py:96-116,
# result_judge.py:943-946) is preserved because the retry gets a fresh
# directory.
RESAMPLEABLE_JUDGE_STATES = {"model_output_invalid"}
JUDGE_RESAMPLE_ATTEMPTS = 1
# Everything the shared judge (result_judge.py:885-1016), the shared scorer
# (execution_scoring.py:96-119) and the task-local once-guards author inside the
# judge directory.  Any OTHER file there was written by this caller before the
# judge ran and is restored byte for byte into the fresh attempt.
JUDGE_AUTHORED_FILES = (
    "scoring_intent.json", "input_manifest.json", "judge_prompt.txt",
    "logical_request_started.json", "provider_response.json",
    "provider_response-attempts.json", "model_response.json",
    "result_eval_result.json", "result_score_contract.json",
    "task_judge_intent.json", "task_judge_invocation.json",
    "task_cli_stdout.log", "task_cli_stderr.log",
)


def refused_judge_state(judge_dir):
    """The judge's own evaluation_state when it answered and refused to publish.

    None keeps today's behaviour untouched.  A resample is offered only when the
    contract proves the apparatus worked: exactly one completed response with
    complete usage accounting, refused over the model's output rather than over
    the infrastructure.  A missing, unreadable, transport-failed or
    ``infrastructure_error`` contract returns None and stays terminal.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        contract = _json.loads(
            (_Path(judge_dir) / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict) or contract.get("contract_valid") is True:
        return None
    usage = contract.get("provider_usage")
    if not isinstance(usage, dict):
        return None
    if usage.get("completed_responses") != 1 or usage.get("usage_complete") is not True:
        return None
    state = contract.get("evaluation_state")
    return state if state in RESAMPLEABLE_JUDGE_STATES else None


def resample_refused_verdict(judge_dir, rejudge, *, recreate_dir=True):
    """Ask the evaluator's own Result judge again, in a new immutable directory.

    Returns the new judge outcome, or None when nothing was resampled and the
    caller must keep the outcome it already holds.  ``recreate_dir`` is False
    for callers whose own once-guard creates the directory with
    ``mkdir(exist_ok=False)``.
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    judge_dir = _Path(judge_dir)
    outcome = None
    for attempt in range(1, JUDGE_RESAMPLE_ATTEMPTS + 1):
        state = refused_judge_state(judge_dir)
        if state is None:
            return outcome
        retired = judge_dir.parent / ("%s.attempt-%03d-%s" % (judge_dir.name, attempt, state))
        if retired.exists() or not judge_dir.is_dir():
            return outcome
        judge_dir.rename(retired)
        restored = []
        if recreate_dir:
            judge_dir.mkdir(parents=True)
            for item in sorted(retired.iterdir()):
                if item.name in JUDGE_AUTHORED_FILES or item.name.startswith("provider_response-attempt-"):
                    continue
                (_shutil.copytree if item.is_dir() else _shutil.copy2)(item, judge_dir / item.name)
                restored.append(item.name)
        (judge_dir.parent / ("judge_resample_%03d.json" % attempt)).write_text(
            _json.dumps({
                "schema_version": "agentswe-edit-judge-resample-v1",
                "attempt": attempt,
                "judge_dir": str(judge_dir),
                "retired_attempt": str(retired),
                "retired_evaluation_state": state,
                "restored_caller_inputs": restored,
                "candidate_product_re_executed": False,
                "lower_agent_calls_re_issued": 0,
                "reason": "the shared Result judge completed one response and refused to publish "
                          "it; the evaluator's own judge is resampled into a new immutable "
                          "directory and the refused attempt is retained",
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outcome = rejudge()
    return outcome


def execution_verdict(record: dict, case_id: str, digest: str) -> tuple[dict, dict | None]:
    spec = importlib.util.spec_from_file_location("agentswe_execution_contract", RESULT_JUDGE.parent / "execution_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verdict = module.classify_candidate_execution(record, case_id=case_id, candidate_digest=digest)
    zero = module.candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=digest) if verdict.get("classification") == "candidate_zero" and verdict.get("contract_valid") else None
    return verdict, zero


def valid_judge_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    if not all(isinstance(usage.get(field), int) and not isinstance(usage.get(field), bool) and usage[field] > 0
               for field in ("input_tokens", "output_tokens", "total_tokens")):
        return False
    attempts = usage.get("transport_attempts")
    return (isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 1
            and usage["total_tokens"] >= max(usage["input_tokens"], usage["output_tokens"]))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject(output: Path, reasons: list[str]) -> int:
    value = {"schema_version": "agentswe-aider-formal-aggregation/v2",
             "formal_result_publishable": False, "code_score_publishable": False,
             "result_axis": "N/A", "code_axis": "N/A", "combined_score": None,
             "reasons": reasons}
    write_json(output, value)
    return 2


def inside(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"evidence path escapes run directory: {resolved}")
    return resolved


def submission_scoped(run_dir: Path, case_dir: Path) -> bool:
    """True when this case directory belongs to exactly ONE accepted submission.

    A public dev case is evaluated once per accepted submission, so every
    artefact the evaluator writes for it -- the task snapshot, the judge output
    directory, the score-cap contract -- must live under that submission's own
    directory or the second submission collides with immutable bytes the first
    one wrote.  Both public layouts this tree has used are submission scoped:

        lifecycle/product_attempts/deliveries/<candidate_digest>/evaluations/<case>   (current)
        lifecycle/evaluations/candidate_<n>/<case>                                    (legacy)

    ``lifecycle/evaluations/hidden/<case>`` is run scoped, which is correct for
    the hidden axis: it is judged exactly once per run.  The layout literals are
    the same ones ``result_task_input`` already enforces.
    """
    run_dir = run_dir.resolve()
    try:
        relative = inside(run_dir, case_dir).relative_to(run_dir).parts
    except ValueError:
        return False
    if relative[:1] == ("lifecycle",):
        relative = relative[1:]
    delivery = (len(relative) == 5 and relative[:2] == ("product_attempts", "deliveries")
                and len(relative[2]) == 64 and all(c in "0123456789abcdef" for c in relative[2])
                and relative[3] == "evaluations")
    candidate = (len(relative) == 3 and relative[0] == "evaluations"
                 and relative[1].startswith("candidate_"))
    return bool(delivery or candidate)


def score_cap_binding(cap_contract: Path, case_id: str) -> dict[str, Any]:
    """Summarise, from its own bytes, the contract handed to the judge.

    Only ``violated`` entries and the binding ceiling are summarised.  Those are
    the sentences the judge was told to apply, each carrying a public
    requirement reference and a reason that states an evaluator observation
    about THIS rollout.  Undecided entries stay out: ``unavailable`` is neither
    proof of violation nor of compliance, and for a case whose own oracle
    expects a pre-plan refusal the exemption text names that case's expected
    terminal class, which is a private oracle expectation and must not travel
    into public feedback.
    """
    value = read_json(cap_contract)
    entries = value.get("entries")
    entries = entries if isinstance(entries, list) else []
    violated = [{"cap_id": item.get("cap_id"), "maximum_score": item.get("maximum_score"),
                 "status": item.get("status"), "requirement_ref": item.get("requirement_ref"),
                 "reason": item.get("reason")}
                for item in entries if isinstance(item, dict) and item.get("status") == "violated"]
    from evaluator import result_score_caps
    return {"issued": True, "case_kind": "dev" if case_id.startswith("dev_") else "hidden",
            "path": str(cap_contract), "sha256": sha256(cap_contract),
            "schema_version": value.get("schema_version"), "entry_count": len(entries),
            "applied_ceiling": result_score_caps.effective_ceiling(value), "violated": violated}


def _task_snapshot(path: Path, content: bytes) -> None:
    """Create evaluator evidence once; existing bytes are never overwritten."""
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != content or path.stat().st_mode & 0o222:
            raise ValueError("existing Result task snapshot differs or is writable")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fchmod(handle.fileno(), 0o444)
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def result_task_input(run_dir: Path, case_dir: Path, case_id: str) -> tuple[Path, dict[str, Any]]:
    """Bind the actual lower task projection to its own evaluator spec.

    case-work is writable by the lower product, including its hash sidecar.
    The spec is outside that mount. Never substitute another delivery's spec
    when the current public attempt has no valid task evidence.
    """
    run_dir = run_dir.resolve()
    case_dir = inside(run_dir, case_dir)
    relative = case_dir.relative_to(run_dir).parts
    owner = run_dir
    if relative[:1] == ("lifecycle",):
        owner = run_dir / "lifecycle"
        relative = relative[1:]
    current = (len(relative) == 5 and relative[:2] == ("product_attempts", "deliveries")
               and len(relative[2]) == 64 and all(c in "0123456789abcdef" for c in relative[2])
               and relative[3:] == ("evaluations", case_id))
    legacy = (len(relative) == 3 and relative[0] == "evaluations" and relative[2] == case_id
              and (relative[1] == "hidden" or relative[1].startswith("candidate_")))
    if not current and not legacy:
        raise ValueError("unrecognized Result case ownership layout")
    spec_path = (case_dir.parent.parent if current else owner) / f"spec_{case_id}.json"
    expected = None
    expected_digest = None
    if spec_path.is_file():
        if inside(owner, spec_path) != spec_path or spec_path.is_symlink():
            raise ValueError("Result task spec is not evaluator-owned case evidence")
        spec = read_json(spec_path)
        if spec.get("case_id") != case_id:
            raise ValueError("Result task spec case binding differs")
        task = str(spec.get("task_input") or spec.get("scenario") or "")
        expected_digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
        if not task or spec.get("task_input_sha256") != expected_digest:
            raise ValueError("Result task spec content digest differs")
        expected = (task + ("\n" if not task.endswith("\n") else "")).encode("utf-8")
    projected = case_dir / "case-work/task_input.md"
    if projected.exists() or projected.is_symlink():
        if inside(case_dir, projected) != projected or projected.is_symlink():
            raise ValueError("lower task projection escapes its case")
        sidecar = projected.with_suffix(".sha256")
        if expected is None:
            raise ValueError("lower task projection has no bound evaluator spec")
        if projected.read_bytes() != expected:
            raise ValueError("lower task projection differs from evaluator spec")
        if (not sidecar.is_file() or sidecar.is_symlink() or inside(case_dir, sidecar) != sidecar
                or sidecar.read_text(encoding="ascii").strip() != expected_digest):
            raise ValueError("lower task projection sidecar differs from evaluator spec")
        source = projected
        content = expected
        kind = "lower_projection_verified_against_case_spec"
    elif current:
        raise FileNotFoundError("current public attempt lower task projection is missing")
    else:
        source = case_dir / "task_input.md"
        if source.exists() or source.is_symlink():
            if inside(case_dir, source) != source or source.is_symlink():
                raise ValueError("legacy task input escapes its case")
            content = source.read_bytes()
            if not content or (expected is not None and content != expected):
                raise ValueError("legacy task input differs from its case spec")
            kind = "legacy_case_scoped_task"
        elif expected is not None:
            source, content, kind = spec_path, expected, "legacy_case_spec_task_projection"
        else:
            raise FileNotFoundError(f"missing task input for {case_id}")
    snapshot = case_dir / "result_task_input.md"
    binding = {"schema_version": "agentswe-aider-result-task-input/v1", "case_id": case_id,
               "binding": kind, "source": str(source), "source_sha256": sha256(source),
               "spec_path": str(spec_path) if expected is not None else None,
               "spec_sha256": sha256(spec_path) if expected is not None else None,
               "task_input_sha256": expected_digest,
               "snapshot": str(snapshot), "snapshot_sha256": hashlib.sha256(content).hexdigest()}
    _task_snapshot(snapshot, content)
    _task_snapshot(case_dir / "result_task_input_binding.json",
                   (json.dumps(binding, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return snapshot, binding


def run_result_judge(run_dir: Path, case_id: str, record: dict[str, Any], judge_endpoint: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    case_dir = inside(run_dir, Path(str(record["result"])).parent)
    result_path = inside(run_dir, Path(str(record["result"])))
    trajectory = case_dir / "lower.stdout.log"
    trajectory_evidence = case_dir / "trajectory.log"
    native = case_dir / "native_evidence.json"
    oracle = case_dir / "oracle_comparison.json"
    artifact = case_dir / "agent_artifact.json"
    required_evidence = {"trajectory": trajectory, "trajectory_evidence": trajectory_evidence, "native": native, "oracle": oracle}
    missing = [name for name, path in required_evidence.items() if not path.is_file()]
    if missing:
        raise ValueError(f"missing evaluator evidence for {case_id}: {missing}")
    if not artifact.is_file():
        raise ValueError(f"model-authored agent artifact is missing for {case_id}")
    artifact_contract = record.get("artifact_contract")
    if not isinstance(artifact_contract, dict):
        raise ValueError(f"artifact provenance contract is missing for {case_id}")
    if artifact_contract.get("source") != "lower_product_workspace":
        raise ValueError(f"artifact source is not the lower product workspace for {case_id}")
    if artifact_contract.get("evaluator_synthesized") is not False:
        raise ValueError(f"evaluator-synthesized artifact is forbidden for {case_id}")
    if artifact_contract.get("copied_after_lower_exit") is not True:
        raise ValueError(f"artifact copy timing is not attested for {case_id}")
    if artifact_contract.get("candidate_mount_read_only") is not True:
        raise ValueError(f"candidate source mount is not read-only for {case_id}")
    if artifact_contract.get("preexisting_before_launch") is not False:
        raise ValueError(f"agent artifact was not proven absent before lower launch for {case_id}")
    if artifact_contract.get("trajectory_artifact_reference") is not True:
        raise ValueError(f"lower trajectory does not attest artifact authorship for {case_id}")
    write_evidence = artifact_contract.get("write_evidence")
    if not isinstance(write_evidence, dict) or write_evidence.get("accepted") is not True:
        raise ValueError(f"structured Aider edit evidence is missing for {case_id}")
    broker = record.get("broker") if isinstance(record.get("broker"), dict) else {}
    if int(broker.get("successful_calls", 0) or 0) <= 0:
        raise ValueError(f"artifact is not bound to a successful lower-model call for {case_id}")
    if not isinstance(artifact_contract.get("trajectory_digest"), str) or not artifact_contract.get("trajectory_digest"):
        raise ValueError(f"artifact trajectory digest is missing for {case_id}")
    if record.get("trajectory_digest") != artifact_contract.get("trajectory_digest"):
        raise ValueError(f"artifact trajectory digest is not bound for {case_id}")
    if hashlib.sha256(trajectory_evidence.read_bytes()).hexdigest() != record.get("trajectory_digest"):
        raise ValueError(f"artifact trajectory digest does not match persisted trajectory for {case_id}")
    if artifact_contract.get("copy_path") != str(artifact):
        raise ValueError(f"artifact copy path is not bound to the case evidence for {case_id}")
    if artifact_contract.get("exists") is not True or artifact_contract.get("valid_json") is not True:
        raise ValueError(f"artifact existence/JSON contract is invalid for {case_id}")
    if artifact_contract.get("case_id_matches") is not True:
        raise ValueError(f"artifact provenance case binding is invalid for {case_id}")
    artifact_value = read_json(artifact)
    if artifact_value.get("case_id") != case_id:
        raise ValueError(f"agent artifact case mismatch for {case_id}")
    if artifact_value.get("schema_version") != "agentswe-aider-agent-result/v1":
        raise ValueError(f"agent artifact schema version is invalid for {case_id}")
    if artifact_value.get("evaluator_synthesized") is True or artifact_value.get("source") == "evaluator":
        raise ValueError(f"evaluator-synthesized artifact is forbidden for {case_id}")
    if not isinstance(artifact_value.get("decision"), dict) or not isinstance(artifact_value.get("observations"), (dict, list)):
        raise ValueError(f"malformed model-authored artifact for {case_id}")
    if not isinstance(artifact_value.get("integrity"), dict) or not isinstance(artifact_value.get("safety"), dict):
        raise ValueError(f"agent artifact integrity/safety sections are missing for {case_id}")
    if record.get("artifact_sha256") != sha256(artifact):
        raise ValueError(f"agent artifact digest mismatch for {case_id}")
    task_input, task_binding = result_task_input(run_dir, case_dir, case_id)
    rubric = ROOT / "evaluator/rubric.md"
    output = case_dir / "result_judge" if case_id.startswith("dev_") else run_dir / "formal_scoring/result_axis" / case_id
    command = [sys.executable, str(RESULT_JUDGE), "--case-id", case_id,
               "--task-input", str(task_input), "--rubric", str(rubric),
               "--agent-artifact", str(artifact), "--trajectory", str(trajectory),
               "--native-evidence", str(native), "--oracle-summary", str(oracle),
               "--broker-endpoint", judge_endpoint, "--broker-placeholder", "broker-only-placeholder",
               "--output-dir", str(output),
               "--timeout", "2400", "--max-transport-attempts", "4"]
    # Evidence-bound task-local ceilings.  As of 2026-09-21 the public dev round
    # and the hidden round are scored under the SAME contract, built by the same
    # deterministic builder from each case's own evaluator evidence.
    #
    # Before this round the dev path issued nothing, so the Builder read 95 or 98
    # on a dev case while the very same evaluator fact held the hidden round to a
    # ceiling -- e.g. `content_addressed_object_store_valid=false` is
    # c10_object_store_not_content_addressed (30) on hidden, and dev_001 of
    # submission 2 in 0905-edit-codex-xhigh-0921-v4-001-aider scored 98 under it.
    # Feedback that disagrees with the measurement is not feedback; Create already
    # runs one scorer with one ceiling over dev and hidden.
    #
    # The contract is immutable evidence that must sit beside the judge output it
    # was handed to.  For a hidden case that is the single hidden case directory;
    # for a dev case it MUST be this submission's own delivery directory, because
    # a dev case is judged once per accepted submission and a run-root path scoped
    # only by case would be written up to max_dev_rounds times.  `case_dir` is
    # already that per-submission directory -- it is the parent of the
    # per-submission judge output `case_dir/"result_judge"` chosen above -- and
    # submission_scoped() refuses to issue rather than contend for one path.
    #
    # A failure to issue is recorded and never invalidates the case: the judge
    # then scores with no ceiling, exactly as before this round.
    cap_contract = case_dir / "result_score_caps.json"
    case_kind = "dev" if case_id.startswith("dev_") else "hidden"
    cap_binding: dict[str, Any] = {"issued": False, "case_kind": case_kind}
    try:
        if case_kind == "dev" and not submission_scoped(run_dir, case_dir):
            raise ValueError(
                f"dev score-cap contract path is not scoped to one submission: {case_dir}")
        from evaluator import result_score_caps
        contract_value = result_score_caps.build_contract(
            case_id, rubric=rubric, native_evidence=native, oracle_summary=oracle,
            artifact=artifact)
        result_score_caps.write_contract(cap_contract, contract_value)
        # Summarise from the written bytes BEFORE the judge is told to use them:
        # if the summary cannot be built the contract is not handed over either,
        # so the record and the public feedback never disagree with the judge.
        cap_binding = score_cap_binding(cap_contract, case_id)
        command.extend(["--score-cap-contract", str(cap_contract)])
    except Exception as exc:
        cap_binding = {"issued": False, "case_kind": case_kind,
                       "error": f"{type(exc).__name__}: {exc}"}
        write_json(case_dir / "result_score_caps_error.json",
                   {"schema_version": "agentswe-aider-score-cap-issue/v1", "case_id": case_id,
                    "issued": False, "error": f"{type(exc).__name__}: {exc}"})
    def rejudge():
        return subprocess.run(command, text=True, capture_output=True, check=False)

    completed = rejudge()
    again = resample_refused_verdict(output, rejudge)
    if again is not None:
        completed = again
    contract_path = output / "result_score_contract.json"
    contract = read_json(contract_path) if contract_path.is_file() else {
        "evaluation_state": "infrastructure_error", "errors": [completed.stderr[-1200:]]}
    contract["judge_invocation"] = {"entry": str(RESULT_JUDGE), "exit_code": completed.returncode,
                                    "stdout_tail": completed.stdout[-1200:], "stderr_tail": completed.stderr[-1200:]}
    contract["task_input_binding"] = task_binding
    # Always present, so "no ceiling applied" and "no contract issued" are
    # distinguishable in the record and in public dev feedback.
    contract["score_cap_binding"] = cap_binding
    write_json(output / "result_score_contract.json", contract)
    return contract


def valid_result_contract(contract: dict[str, Any], case_id: str) -> bool:
    judge = contract.get("judge") if isinstance(contract.get("judge"), dict) else {}
    usage = contract.get("provider_usage") if isinstance(contract.get("provider_usage"), dict) else {}
    return (contract.get("contract_valid") is True and contract.get("result_score_publishable") is True
            and contract.get("case_id") == case_id and judge.get("model") == "deepseek-flash"
            and judge.get("reasoning_effort") == "max"
            and usage.get("logical_requests") == (2 if contract.get("early_stop_resample") else 1)
            and usage.get("completed_responses") == (2 if contract.get("early_stop_resample") else 1)
            and valid_judge_usage(usage)
            and isinstance(contract.get("result_score"), int) and 0 <= contract["result_score"] <= 100)


def formal_result(run_dir: Path, hidden: dict[str, Any], judge_endpoint: str, *, cases: tuple[str, ...] = CASE_IDS) -> tuple[dict[str, Any] | str, list[str]]:
    reasons: list[str] = []
    records = hidden.get("results")
    if not isinstance(records, list) or [item.get("case_id") for item in records if isinstance(item, dict)] != list(cases):
        return "N/A", ["hidden attestation does not contain canonical six-case records"]
    contracts: dict[str, Any] = {}
    scores: dict[str, int] = {}
    for item in records:
        case_id = str(item["case_id"])
        classification = str(item.get("classification") or "")
        broker = item.get("broker") if isinstance(item.get("broker"), dict) else {}
        if (classification in INFRA or item.get("infrastructure_invalid") is True
                or int(broker.get("failures_delta", 0) or 0) != 0):
            reasons.append(f"{case_id}: infrastructure-invalid/N/A")
            continue
        raw = read_json(inside(run_dir, Path(str(item["result"]))))
        verdict, zero = execution_verdict(raw, case_id, str(hidden.get("candidate_digest", "")))
        if zero is not None:
            contracts[case_id] = zero
            scores[case_id] = 0
            write_json(run_dir / "formal_scoring/result_axis" / case_id / "result_score_contract.json", zero)
            continue
        if verdict.get("classification") == "infrastructure_invalid":
            reasons.append(f"{case_id}: {verdict.get('reason')}")
            continue
        contract = run_result_judge(run_dir, case_id, item, judge_endpoint)
        contracts[case_id] = contract
        if not valid_result_contract(contract, case_id):
            reasons.append(f"{case_id}: shared Result judge contract is invalid")
            continue
        scores[case_id] = int(contract["result_score"])
    write_json(run_dir / "formal_scoring/result_axis/result_judge_contracts.json", contracts)
    if reasons or set(scores) != set(cases):
        return {"score": None, "maximum": 100, "aggregation": "six-case arithmetic mean", "case_scores": scores,
                "judge_contracts": contracts}, reasons or ["not all six hidden cases are scoreable"]
    return {"score": round(sum(scores.values()) / len(cases), 4), "maximum": 100,
            "aggregation": "arithmetic mean of selected independent hidden cases", "case_scores": scores,
            "judge_contracts": contracts}, []


def run_code_judge(run_dir: Path, frozen: dict[str, Any], credential: Path, *, preflight_only=False) -> dict[str, Any]:
    source, requirements, code_digest, evidence_paths = prepare_code_inputs(run_dir, frozen)
    output = run_dir / ('formal_scoring/code_preflight' if preflight_only else 'formal_scoring/code_axis')
    command = [sys.executable, str(CODE_JUDGE), "--candidate-source", str(source),
               "--public-requirements", str(requirements), "--code-rubric", str(ROOT / "meta/code_rubric.md"),
               "--credential-file", str(credential), "--output-dir", str(output),
               "--expected-candidate-digest", code_digest,
               "--timeout", "2400", "--transport-mode", "stream"]
    for path in evidence_paths:
        command.extend(['--evidence-path', path])
    if preflight_only:
        command.append('--preflight-only')
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if preflight_only:
        value = {'provider_calls': 0, 'preflight_valid': completed.returncode == 0,
                 'candidate_digest': frozen['candidate_digest'], 'code_full_tree_digest': code_digest,
                 'stdout': completed.stdout, 'stderr': completed.stderr, 'exit_code': completed.returncode}
        write_json(run_dir / 'code_preflight.json', value)
        if completed.returncode:
            raise ValueError('Code source/runtime preflight failed before lower launch')
        return value
    contract_path = output / "code_score_contract.json"
    value = read_json(contract_path) if contract_path.is_file() else {"evaluation_state": "infrastructure_error"}
    value["judge_invocation"] = {"entry": str(CODE_JUDGE), "exit_code": completed.returncode,
                                 "stdout_tail": completed.stdout[-1200:], "stderr_tail": completed.stderr[-1200:]}
    write_json(output / 'judge_invocation.json', value['judge_invocation'])
    if value.get('candidate_digest') != code_digest:
        value['contract_valid'] = False
        value['code_score_publishable'] = False
        value.setdefault('errors', []).append('Code contract full-source identity mismatch')
    return value


def finalize(run_dir: Path, credential: Path, judge_endpoint: str, *, acceptance_cases: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    acceptance = acceptance_cases is not None
    cases = tuple(acceptance_cases) if acceptance else CASE_IDS
    if not cases or len(set(cases)) != len(cases) or any(case not in CASE_IDS for case in cases):
        raise ValueError("acceptance cases must be a nonempty unique hidden subset")
    output = run_dir / ("acceptance_aggregation.json" if acceptance else "formal_aggregation.json")
    reasons: list[str] = []
    session_path = run_dir / "builder_session_attestation.json"
    if acceptance and not session_path.is_file():
        session_path = run_dir / "pilot_builder_session_attestation.json"
    reuse_path = run_dir / "acceptance_source.json"
    reuse = acceptance and reuse_path.is_file()
    if reuse:
        session_path = reuse_path
    freeze_path = run_dir / "freeze_manifest.json"
    hidden_path = run_dir / "hidden-after-freeze-attestation.json"
    if not freeze_path.is_file():
        freeze_path = run_dir / "lifecycle/freeze_manifest.json"
    if not hidden_path.is_file():
        hidden_path = run_dir / "lifecycle/hidden-after-freeze-attestation.json"
    for path in (session_path, freeze_path, hidden_path):
        if not path.is_file():
            reasons.append(f"missing required lifecycle evidence: {path.name}")
    if reasons:
        return reject(output, reasons), read_json(output)
    session, freeze, hidden = map(read_json, (session_path, freeze_path, hidden_path))
    if not reuse and (session.get("same_session") is not True or session.get("accepted_rounds", session.get("accepted_submission_count", session.get("accepted_candidate_count", 0))) < 1):
        reasons.append("Builder was not one continuous session with an accepted round")
    lifecycle_path = run_dir / "lifecycle" / "dev_lifecycle.json"
    lifecycle = read_json(lifecycle_path) if lifecycle_path.is_file() else {}
    ledger = lifecycle.get("records") if isinstance(lifecycle, dict) else None
    if not reuse and (not isinstance(ledger, list) or not 1 <= len(ledger) <= 10):
        reasons.append("accepted-submission ledger is missing or outside 1..10")
        ledger = []
    if reuse:
        ledger = []
        if (session.get("mode") != "acceptance_reuse" or session.get("source_modified") is not False
                or session.get("builder_lifecycle_replayed") is not False
                or session.get("candidate_digest") != freeze.get("candidate_digest")):
            reasons.append("acceptance source is not an immutable explicitly non-Builder diagnostic copy")
        sys.path.insert(0, str(ROOT))
        from harbor.agentloop_controller import digest as candidate_tree_digest
        source = inside(run_dir, str(freeze.get("frozen_candidate_path", "")))
        if not source.is_dir() or candidate_tree_digest(source) != freeze.get("candidate_digest"):
            reasons.append("acceptance frozen product digest mismatch")
        if any((p.is_symlink() and source not in p.resolve().parents)
               or (not p.is_symlink() and p.stat().st_mode & 0o222) for p in source.rglob("*")):
            reasons.append("acceptance frozen product is writable or contains escaping symlinks")
    digests = [item.get("candidate_digest") for item in ledger if isinstance(item, dict)]
    if len(digests) != len(set(digests)):
        reasons.append("accepted Candidate delivery digests are not unique")
    if not reuse and (freeze.get("accepted_submission_count") != len(ledger) or freeze.get("accepted_candidate_digests") != digests):
        reasons.append("freeze manifest is not bound to the complete accepted ledger")
    if ledger and freeze.get("candidate_delivery_digest") != digests[-1]:
        reasons.append("freeze is not bound to the latest accepted Candidate")
    if not reuse and (freeze.get("feedback_consumed") is not True or freeze.get("feedback_chain_complete") is not True):
        reasons.append("freeze does not prove complete feedback consumption")
    if freeze.get("hidden_started_after_freeze") is not True and hidden.get("started_after_freeze") is not True:
        reasons.append("hidden execution was not proven post-freeze")
    if freeze.get("candidate_digest") != freeze.get("build_digest") and freeze.get("build_digest"):
        reasons.append("frozen digest/build digest mismatch")
    if freeze.get("freeze_reason") not in ({"acceptance_reuse"} if reuse else {"builder_exit", "max_dev_rounds"}):
        reasons.append("freeze reason is not builder_exit or max_dev_rounds")
    if hidden.get("expected_cases") != list(cases) or hidden.get("executed_cases") != list(cases):
        reasons.append("hidden inventory is not exactly six canonical cases")
    if hidden.get("pilot_not_formal") is True and not acceptance:
        reasons.append("single-case pilot cannot claim formal")
    freeze_digest = freeze.get("candidate_digest")
    if (hidden.get("frozen_digest_stable") is not True
            or hidden.get("frozen_digest_before") != freeze_digest
            or hidden.get("frozen_digest_after") != freeze_digest):
        reasons.append("hidden evidence does not prove measured frozen-source stability")
    if reasons:
        return reject(output, reasons), read_json(output)
    try:
        result_axis, result_reasons = formal_result(run_dir, hidden, judge_endpoint, cases=cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result_axis, result_reasons = "N/A", [f"hidden evidence/artifact rejected: {type(exc).__name__}: {exc}"]
    code_contract = {"evaluation_state": "skipped_by_policy", "policy": {"id": "edit-code-axis-retired-2026-09-19"}, "reason": "Result-only evaluation; Code judge not dispatched", "code_score": None}  # Code axis retired 2026-09-19 (Result-only)
    code_ok = True
    if result_reasons:
        result = {"schema_version": "agentswe-aider-formal-aggregation/v2", "formal_result_publishable": False,
                  "code_score_publishable": False, "result_axis": "N/A", "code_axis": code_contract if code_ok else "N/A",
                  "combined_score": None, "reasons": result_reasons}
        write_json(output, result)
        return 2, result
    result = {"schema_version": "agentswe-aider-formal-aggregation/v2", "formal_result_publishable": not acceptance,
              "mode": "acceptance" if acceptance else "formal", "selected_cases": list(cases),
              "acceptance_result_publishable": acceptance, "acceptance_complete": acceptance and code_ok,
              "code_score_publishable": False, "result_axis": result_axis,
              "code_axis": code_contract if code_ok else "N/A", "combined_score": None,
              "result_judge_contracts": str(run_dir / "formal_scoring/result_axis/result_judge_contracts.json"),
              "code_contract": str(run_dir / "formal_scoring/code_axis/code_score_contract.json")}
    write_json(output, result)
    return (0 if code_ok else 2), result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--result-judge-broker-endpoint", required=True)
    parser.add_argument("--acceptance-cases", nargs="+")
    args = parser.parse_args()
    code, value = finalize(args.run_dir.resolve(), args.credential_file.resolve(), args.result_judge_broker_endpoint, acceptance_cases=args.acceptance_cases)
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
