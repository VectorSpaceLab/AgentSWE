#!/usr/bin/env python3
"""Finalize Codex Edit Result/Code using evaluator-owned semantic judges."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluator.code_inputs import prepare_code_inputs
from evaluator.judge_trajectory_projection import project as project_trajectory  # D6 bounded judge projection
RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
# The runner above is only the evaluator-owned boundary around the unchanged
# Create Code judge at ``@@AGENTSWE_EDITING_CONTROL@@/code_eval.py``.
# Keep the authority explicit for static contract/readiness audits.
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
CASES = [f"test_{index:03d}" for index in range(1, 7)]
INFRA = {"infrastructure-invalid", "provider_infrastructure_failure", "broker_infrastructure_failure",
         "launcher_infrastructure_failure", "mount_infrastructure_failure", "evaluator_infrastructure_failure"}


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
    path = RESULT_JUDGE.parent / "execution_contract.py"
    spec = importlib.util.spec_from_file_location("agentswe_execution_contract", path)
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
            and usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"])


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_value(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def immutable_text(path: Path, text: str) -> None:
    """Reuse identical evaluator input only; never replace scoring evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"immutable scoring input is a symlink: {path}")
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(text)
    except FileExistsError:
        if not path.is_file() or path.read_text(encoding='utf-8') != text:
            raise ValueError(f"immutable scoring input changed: {path}")


def invoke_once(command: list[str], output: Path, input_paths: list[Path]) -> dict[str, Any]:
    """Claim a fresh task scoring phase, preserving the raw judge contract.

    The shared judge owns transport retries. A task adapter must not interpret
    a stale contract after a failed CLI invocation, nor silently invoke again.
    """
    inputs = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
              for path in input_paths]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    write_json(output / 'task_judge_intent.json', {'schema_version': 'agentswe-task-judge-intent-v1',
        'command': command, 'inputs': inputs, 'logical_invocation_limit': 1})
    try:
        done = subprocess.run(command, text=True, capture_output=True, check=False)
        (output / 'task_cli_stdout.log').write_text(done.stdout, encoding='utf-8')
        (output / 'task_cli_stderr.log').write_text(done.stderr, encoding='utf-8')
        evidence = {'entry': command[1], 'exit_code': done.returncode,
            'stdout_tail': done.stdout[-1200:], 'stderr_tail': done.stderr[-1200:]}
    except Exception as error:
        evidence = {'entry': command[1], 'exit_code': None,
            'error_type': type(error).__name__, 'error': str(error)}
    write_json(output / 'task_judge_invocation.json', evidence)
    return evidence


def checked_raw_contract(output: Path, name: str, invocation: dict[str, Any], *,
                         expected_digest: str | None = None) -> dict[str, Any]:
    path = output / name
    reason = None
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    try:
        contract = read_json(path)
    except (OSError, ValueError):
        contract = {}
        reason = 'judge did not produce a readable contract'
    if invocation.get('exit_code') != 0:
        reason = 'judge CLI did not complete successfully'
    elif expected_digest is not None and contract.get('candidate_digest') != expected_digest:
        reason = 'Code contract materialized full-source identity mismatch'
    if reason:
        # This is a task-level invalidity wrapper, not a rewrite of the judge.
        return {'evaluation_state': 'infrastructure_error', 'contract_valid': False,
            'result_score_publishable': False, 'code_score_publishable': False,
            'reason': reason, 'raw_contract_path': str(path), 'raw_contract_sha256': raw_hash,
            'task_invocation_path': str(output / 'task_judge_invocation.json')}
    return contract


def reject(path: Path, reasons: list[str]) -> tuple[int, dict[str, Any]]:
    value = {"schema_version": "agentswe-codex-formal-aggregation-v2", "formal_result_publishable": False,
             "code_score_publishable": False, "result_axis": "N/A", "code_axis": "N/A",
             "combined_score": None, "reasons": reasons}
    write_json(path, value)
    return 2, value


def artifact_provenance_error(run_dir: Path, case_id: str, record: dict[str, Any], *, case_dir: Path | None = None) -> str | None:
    """Require the exact artifact emitted by the lower agent before judging."""
    case_dir = case_dir or run_dir / "evaluations/hidden" / case_id
    artifact = case_dir / "agent_artifact.json"
    if not artifact.is_file():
        return "agent-authored artifact is missing"
    if record.get("agent_authored_artifact") is not True:
        return "execution record does not attest an agent-authored artifact"
    if record.get("score_kind") != "native_evaluator_measurement":
        return "execution record has no explicit native-measurement boundary"
    if record.get("formal_result_publishable") is not False or record.get("code_score_publishable") is not False:
        return "native execution record incorrectly claims a formal score"
    provenance = record.get("artifact_provenance")
    if not isinstance(provenance, dict) or provenance.get("evaluator_synthesized") is not False:
        return "agent artifact provenance is missing or evaluator-synthesized"
    if provenance.get("source_path") != "workspace/agent_result.json":
        return "agent artifact source is not the lower-agent workspace artifact"
    if provenance.get("preexisting_before_launch") is not False:
        return "agent artifact was not proven absent before lower launch"
    if not isinstance(provenance.get("broker_successful_calls"), int) or provenance.get("broker_successful_calls", 0) <= 0:
        return "agent artifact is not bound to successful lower-model use"
    if provenance.get("trajectory_artifact_reference") is not True:
        return "lower trajectory does not reference the agent artifact"
    if provenance.get("trajectory_artifact_write_event") is not True:
        return "lower trajectory has no artifact write event"
    trajectory = case_dir / "candidate.stdout.jsonl"
    if not trajectory.is_file():
        return "lower trajectory is missing"
    if not isinstance(record.get("trajectory_digest"), str) or not record.get("trajectory_digest"):
        return "lower trajectory digest is missing"
    measured_digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
    if measured_digest != record.get("trajectory_digest"):
        return "lower trajectory digest does not match persisted trajectory"
    if provenance.get("trajectory_digest") != record.get("trajectory_digest"):
        return "lower trajectory digest is not bound to artifact provenance"
    if provenance.get("case_id") != case_id or record.get("artifact_sha256") != provenance.get("sha256"):
        return "agent artifact case binding or digest attestation is inconsistent"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != record.get("artifact_sha256"):
        return "agent artifact digest does not match the copied artifact"
    try:
        value = read_json(artifact)
    except (OSError, ValueError, json.JSONDecodeError):
        return "agent artifact is not parseable JSON"
    if value.get("schema_version") != "agentswe-codex-residual-agent-result/v1" or value.get("case_id") != case_id:
        return "agent artifact schema or case binding is invalid"
    return None


# D23 malformed artifact: exactly the two findings above that are read out of the
# Candidate's own agent_artifact.json bytes (unparseable JSON; wrong schema_version or
# wrong case_id inside the artifact).  The disclosed task contract requires that file, so
# a product that writes it wrongly has produced a defective delivery for THAT case.  Every
# other return of artifact_provenance_error is an evaluator-recorded fact (the artifact or
# the trajectory is missing, the execution record does not attest authorship, a recorded
# digest disagrees) and stays an axis reason.
CANDIDATE_ARTIFACT_FINDINGS = frozenset({
    "agent artifact is not parseable JSON",
    "agent artifact schema or case binding is invalid",
})
CANDIDATE_ARTIFACT_FATAL_CLASS = "candidate_artifact_failure"


def candidate_artifact_zero(run_dir: Path, case_id: str, record: dict[str, Any], digest: str,
                            finding: str, *, case_dir: Path | None = None) -> dict[str, Any] | None:
    """That case's evidenced Candidate zero, or None to leave today's behaviour alone.

    The contract is written by the evaluator-owned execution_contract module, so it is
    byte-for-byte the record the execution-side Candidate zeros already produce; the
    finding is carried verbatim in the reason.
    """
    if finding not in CANDIDATE_ARTIFACT_FINDINGS:
        return None
    case_dir = case_dir or run_dir / "evaluations/hidden" / case_id
    artifact = case_dir / "agent_artifact.json"
    if not artifact.is_file() or record.get("case_id") != case_id or not digest:
        return None
    path = RESULT_JUDGE.parent / "execution_contract.py"
    spec = importlib.util.spec_from_file_location("agentswe_execution_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if CANDIDATE_ARTIFACT_FATAL_CLASS not in module.FATAL_CANDIDATE_CLASSES:
        return None
    verdict = {"case_id": case_id, "candidate_digest": digest,
               "classification": "candidate_zero", "score": 0,
               "contract_valid": True, "round_consumed": True,
               "reason": f"{CANDIDATE_ARTIFACT_FATAL_CLASS}: {finding}",
               "evidence": [{"path": str(artifact.resolve()),
                             "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    return module.candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=digest)


def score_cap_path(run_dir: Path, case_id: str, output: Path) -> Path:
    """Where this judging's evaluator-issued cap contract lives.

    2026-09-20 (round 3).  The contract binds the digests of *this* judging's
    native evidence and oracle summary, so its text differs for every Builder
    submission of the same dev case.  A run-root path scoped only by case is
    therefore immutable across exactly one submission: the second one to reach
    the judge dies in ``immutable_text``.  That is what happened in
    0905-edit-codex-xhigh-0920-fh-001-codex, where submission 4 was destroyed six
    times by

        ValueError: immutable scoring input changed:
          <run>/result_score_caps_dev_001.json

    and the run then froze on submission 3 with ``freeze_reason: builder_exit``,
    scoring a product one feedback round less mature than its budget allowed.

    A dev contract therefore lives beside that submission's judge output, which
    is already per-submission, next to the artifact and oracle summary it binds.
    The hidden path is deliberately unchanged: there is exactly one hidden
    execution per run, and the run-root ``result_score_caps_<case>.json`` file is
    the evidence every downstream reader cites.
    """
    if case_id.startswith("dev_"):
        return output.parent / f"result_score_caps_{case_id}.json"
    return run_dir / f"result_score_caps_{case_id}.json"


def judge_case(run_dir: Path, case_id: str, record: dict[str, Any], endpoint: str, *, case_dir: Path | None = None) -> dict[str, Any]:
    case_dir = case_dir or run_dir / "evaluations/hidden" / case_id
    error = artifact_provenance_error(run_dir, case_id, record, case_dir=case_dir)
    if error:
        raise ValueError(f"{case_id}: {error}")
    task_input = run_dir / f"task_input_{case_id}.txt"
    immutable_text(task_input, (ROOT / ("dev_cases" if case_id.startswith("dev_") else "test_cases") /
                   case_id / "input.md").read_text(encoding="utf-8"))
    artifact = case_dir / "agent_artifact.json"
    if not artifact.is_file():
        raise ValueError(f"{case_id}: agent-authored artifact is required before shared Result judging")
    # D6 (2026-09-19): judge receives the bounded projection; original trajectory digest was verified above.
    trajectory, _projection_manifest = project_trajectory(case_dir / "candidate.stdout.jsonl")
    native = case_dir / "native_evidence.json"
    oracle = case_dir / "oracle_comparison.json"
    rubric = ROOT / "evaluator/rubric.md"
    output = case_dir / "result_judge" if case_id.startswith("dev_") else run_dir / "formal_scoring/result_axis" / case_id
    from evaluator.score_caps import build_contract
    caps = score_cap_path(run_dir, case_id, output)
    immutable_text(caps, json.dumps(build_contract(case_id, rubric, native, oracle), indent=2) + "\n")
    command = [sys.executable, str(RESULT_JUDGE), "--case-id", case_id, "--task-input", str(task_input),
               "--rubric", str(rubric), "--rubric-dimensions", str(ROOT / 'evaluator/result_dimensions.json'),
               "--agent-artifact", str(artifact), "--trajectory", str(trajectory),
               "--native-evidence", str(native), "--oracle-summary", str(oracle),
               "--score-cap-contract", str(caps),
               "--broker-endpoint", endpoint, "--broker-placeholder", "broker-only-placeholder",
               "--output-dir", str(output), "--timeout", "2400", "--max-transport-attempts", "4"]
    def rejudge():
        return invoke_once(command, output, [task_input, rubric, ROOT / 'evaluator/result_dimensions.json',
                           artifact, trajectory, native, oracle, caps])

    settled_path = output / 'result_score_contract.json'
    if settled_path.is_file():
        # finalizer-reuse-guard: this case already holds a bound verdict from an earlier
        # pass. Judging again would break invoke_once()'s mkdir(exist_ok=False) once-guard
        # AND spend a second logical request on a case that already has one, so a
        # publishable contract is reused exactly as it stands. A refused one goes to the
        # same resample path used below, which retires it and judges once into the freed
        # directory. Nothing measured is repeated.
        try:
            settled = read_json(settled_path)
        except (OSError, ValueError):
            settled = {}
        if valid(settled, case_id):
            return settled
        again = resample_refused_verdict(output, rejudge, recreate_dir=False)
        if again is None:
            return settled
        return checked_raw_contract(output, 'result_score_contract.json', again)

    invocation = rejudge()
    # invoke_once() creates `output` with mkdir(exist_ok=False) and the score-cap
    # contract lives outside it, so the retired attempt is not recreated here.
    again = resample_refused_verdict(output, rejudge, recreate_dir=False)
    if again is not None:
        invocation = again
    return checked_raw_contract(output, 'result_score_contract.json', invocation)


def valid(contract: dict[str, Any], case_id: str) -> bool:
    judge = contract.get("judge") if isinstance(contract.get("judge"), dict) else {}
    usage = contract.get("provider_usage") if isinstance(contract.get("provider_usage"), dict) else {}
    return (contract.get("contract_valid") is True and contract.get("result_score_publishable") is True
            and contract.get("case_id") == case_id and judge.get("model") == "deepseek-flash"
            and judge.get("reasoning_effort") == "max"
            and usage.get("logical_requests") == (2 if contract.get("early_stop_resample") else 1)
            and usage.get("completed_responses") == (2 if contract.get("early_stop_resample") else 1)
            and valid_judge_usage(usage)
            and isinstance(contract.get("result_score"), int))


def run_code_judge(run_dir, freeze, credential, *, preflight_only=False):
    source, requirements, full, paths = prepare_code_inputs(run_dir, freeze)
    output = run_dir / ('formal_scoring/code_preflight' if preflight_only else 'formal_scoring/code_axis')
    command = [sys.executable, str(CODE_JUDGE), '--candidate-source', str(source),
        '--public-requirements', str(requirements), '--code-rubric', str(ROOT / 'evaluator/code_rubric.md'),
        '--credential-file', str(credential), '--output-dir', str(output),
        '--expected-candidate-digest', full, '--timeout', '2400']
    for path in paths:
        command.extend(['--evidence-path', path])
    if preflight_only:
        command.append('--preflight-only')
    invocation = invoke_once(command, output, [ROOT / 'evaluator/code_rubric.md'])
    if preflight_only:
        report = {'preflight_valid': invocation.get('exit_code') == 0, 'provider_calls': 0,
            'delivery_digest': freeze['candidate_digest'], 'product_Code_digest': full,
            **invocation}
        immutable_text(run_dir / 'code_preflight.json', json.dumps(report, indent=2) + '\n')
        if invocation.get('exit_code') != 0:
            raise ValueError('Code source/runtime preflight failed before lower execution')
        return report
    return checked_raw_contract(output, 'code_score_contract.json', invocation, expected_digest=full)


def finalize(run_dir: Path, credential: Path, endpoint: str, *, acceptance_cases: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    cases = acceptance_cases if acceptance_cases is not None else CASES
    acceptance = acceptance_cases is not None
    if not cases or len(cases) != len(set(cases)) or any(case not in CASES for case in cases):
        raise ValueError("acceptance cases must be a unique nonempty subset of hidden cases")
    output = run_dir / ("acceptance_aggregation.json" if acceptance else "formal_aggregation.json")
    freeze_path = run_dir / "freeze_manifest.json"
    lifecycle_path = run_dir / "dev_lifecycle.json"
    diagnostic_path = run_dir / "acceptance_source.json"
    if not freeze_path.is_file() or not (lifecycle_path.is_file() or (acceptance and diagnostic_path.is_file())):
        return reject(output, ["missing freeze or dev lifecycle evidence"])
    freeze, lifecycle = read_json(freeze_path), read_value(lifecycle_path) if lifecycle_path.is_file() else {}
    records = lifecycle.get("records", lifecycle) if isinstance(lifecycle, dict) else lifecycle
    if (not isinstance(records, list) or not records) and not (acceptance and diagnostic_path.is_file()):
        return reject(output, ["no accepted Builder lifecycle records"])
    hidden_path = run_dir / "hidden_attestation.json"
    if acceptance and not hidden_path.is_file():
        hidden_path = run_dir / "pilot_hidden_attestation.json"
    if not hidden_path.is_file():
        return reject(output, ["missing hidden attestation"])
    hidden = read_json(hidden_path)
    if hidden.get("case_inventory") != cases or hidden.get("executed_cases") != cases or (not acceptance and hidden.get("pilot_not_formal") is True):
        return reject(output, ["formal hidden inventory is not exactly six post-freeze cases"])
    freeze_digest = freeze.get("candidate_digest")
    if (hidden.get("frozen_digest_stable") is not True
            or hidden.get("frozen_digest_before") != freeze_digest
            or hidden.get("frozen_digest_after") != freeze_digest):
        return reject(output, ["hidden evidence does not prove measured frozen-source stability"])
    frozen_path = Path(str(freeze.get("path", ""))).resolve()
    if not frozen_path.is_dir():
        return reject(output, ["frozen Candidate path is unavailable for independent digest verification"])
    independent_digest = hashlib.sha256()
    for path in sorted(frozen_path.rglob("*"), key=lambda item: str(item.relative_to(frozen_path))):
        relative = str(path.relative_to(frozen_path)).encode()
        independent_digest.update(len(relative).to_bytes(8, "big"))
        independent_digest.update(relative)
        if path.is_symlink():
            kind, payload = b"L", path.readlink().as_posix().encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        independent_digest.update(kind)
        independent_digest.update(len(payload).to_bytes(8, "big"))
        independent_digest.update(payload)
    if independent_digest.hexdigest() != freeze_digest:
        return reject(output, ["frozen Candidate digest failed independent finalizer verification"])
    if acceptance and diagnostic_path.is_file():
        source = read_json(diagnostic_path)
        if source.get("candidate_digest") != freeze_digest or source.get("mode") != "acceptance_reuse":
            return reject(output, ["acceptance source is not bound to the frozen Candidate"])
    contracts: dict[str, Any] = {}; scores: dict[str, int] = {}; reasons: list[str] = []
    result_records = hidden.get("results", [])
    by_case = {str(item.get("case_id")): item for item in result_records if isinstance(item, dict)}
    for case_id in cases:
        item = by_case.get(case_id)
        if not item:
            reasons.append(f"{case_id}: missing execution record"); continue
        if str(item.get("classification")) in INFRA:
            reasons.append(f"{case_id}: infrastructure-invalid/N/A"); continue
        verdict, zero = execution_verdict(item, case_id, freeze_digest)
        if zero is not None:
            contracts[case_id] = zero
            scores[case_id] = 0
            write_json(run_dir / "formal_scoring/result_axis" / case_id / "result_score_contract.json", zero)
            continue
        if verdict.get("classification") == "infrastructure_invalid":
            reasons.append(f"{case_id}: infrastructure-invalid/N/A: {verdict.get('reason')}"); continue
        provenance_error = artifact_provenance_error(run_dir, case_id, item)
        if provenance_error:
            # D23 malformed artifact: a Candidate-authored artifact defect is THAT case's
            # Candidate zero; it must never void the Result axis for the other five cases.
            artifact_zero = candidate_artifact_zero(run_dir, case_id, item, freeze_digest, provenance_error)
            if artifact_zero is not None:
                contracts[case_id] = artifact_zero
                scores[case_id] = 0
                write_json(run_dir / "formal_scoring/result_axis" / case_id / "result_score_contract.json", artifact_zero)
                continue
            reasons.append(f"{case_id}: {provenance_error}"); continue
        contract = judge_case(run_dir, case_id, item, endpoint); contracts[case_id] = contract
        if valid(contract, case_id):
            scores[case_id] = int(contract["result_score"])
        else:
            reasons.append(f"{case_id}: invalid shared Result judge contract")
    write_json(run_dir / "formal_scoring/result_axis/result_judge_contracts.json", contracts)
    code_dir = run_dir / "formal_scoring/code_axis"
    code_contract_path = code_dir / "code_score_contract.json"
    code_contract = {"evaluation_state": "skipped_by_policy", "policy": {"id": "edit-code-axis-retired-2026-09-19"}, "reason": "Result-only evaluation; Code judge not dispatched", "code_score": None}  # Code axis retired 2026-09-19 (Result-only)
    code_ok = True
    if reasons or set(scores) != set(cases):
        value = {"schema_version": "agentswe-codex-formal-aggregation-v2", "formal_result_publishable": False,
                 "code_score_publishable": False, "result_axis": "N/A", "code_axis": code_contract if code_ok else "N/A",
                 "combined_score": None, "case_scores": scores, "result_judge_contracts": contracts, "reasons": reasons}
        write_json(output, value); return 2, value
    value = {"schema_version": "agentswe-codex-formal-aggregation-v2", "formal_result_publishable": not acceptance,
             "mode": "acceptance" if acceptance else "formal", "acceptance_result_publishable": acceptance,
             "acceptance_complete": acceptance and code_ok, "selected_cases": cases,
             "code_score_publishable": False, "result_axis": {"score": round(sum(scores.values()) / len(cases), 4), "maximum": 100,
             "aggregation": "arithmetic mean of selected independent hidden cases" if acceptance else "arithmetic mean of six independent hidden cases", "case_scores": scores,
             "judge_contracts": contracts}, "code_axis": code_contract if code_ok else "N/A", "combined_score": None,
             "result_judge_contracts": str(run_dir / "formal_scoring/result_axis/result_judge_contracts.json"),
             "code_contract": str(code_contract_path)}
    write_json(output, value); return (0 if code_ok else 2), value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--result-judge-broker-endpoint", required=True)
    parser.add_argument("--acceptance-cases", nargs="+")
    args = parser.parse_args()
    code, value = finalize(args.run_dir.resolve(), args.credential_file.resolve(), args.result_judge_broker_endpoint, acceptance_cases=args.acceptance_cases)
    print(json.dumps(value, indent=2, ensure_ascii=False)); return code


if __name__ == "__main__":
    raise SystemExit(main())
