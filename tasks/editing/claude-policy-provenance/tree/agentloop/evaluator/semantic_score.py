"""One semantic Result boundary shared by public dev and hidden execution."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .lower_agent_launcher import valid_agent_result

SHARED = Path("@@AGENTSWE_EDITING_CONTROL@@")
TASK_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIMENSIONS = TASK_ROOT / 'evaluator/result_dimensions.json'
SCORE_CAPS = TASK_ROOT / 'evaluator/score_caps.py'


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


class SemanticMeasurementUnavailable(RuntimeError):
    """Carry the actual invalid judge contract into the terminal N/A report."""
    def __init__(self, contract_path: Path, contract: dict, exit_code: int):
        super().__init__('Result judge returned no valid semantic measurement')
        self.contract_path, self.contract, self.exit_code = contract_path, contract, exit_code


def execution_verdict(record: dict, case_id: str, digest: str) -> tuple[dict, dict | None]:
    spec = importlib.util.spec_from_file_location("agentswe_execution_contract", SHARED / "execution_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from .execution_provenance import normalize
    try:
        normalized, normalization_evidence = normalize(record, case_id=case_id, candidate_digest=digest)
    except (OSError, ValueError, TypeError, KeyError):
        normalized, normalization_evidence = record, []
    verdict = module.classify_candidate_execution(normalized, case_id=case_id, candidate_digest=digest)
    if normalization_evidence:
        verdict['normalization_evidence'] = normalization_evidence
    zero = module.candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=digest) if verdict.get("classification") == "candidate_zero" and verdict.get("contract_valid") else None
    return verdict, zero


def score_cap_contract(*, case_id: str, rubric: Path, native: Path, oracle: Path,
                      output: Path) -> Path | None:
    """Write this case's evidence-bound Result ceilings next to the judge output.

    The ceilings are derived only from the sanitized oracle comparison the
    evaluator wrote. If the module or the evidence is unavailable the judge is
    invoked exactly as before, without a ceiling contract.
    """
    if not SCORE_CAPS.is_file():
        return None
    spec = importlib.util.spec_from_file_location("agentswe_claude_score_caps", SCORE_CAPS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contract = module.build_contract(case_id, Path(rubric), Path(native), Path(oracle))
    output.mkdir(parents=True, exist_ok=True)
    path = output / "result_score_caps.json"
    path.write_text(json.dumps(contract, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def judge_evidence(*, case_id: str, artifact: Path, trajectory: Path, task_input: Path,
                   rubric: Path, native: Path, oracle: Path, endpoint: str, output: Path) -> dict[str, Any]:
    if not endpoint:
        raise RuntimeError("independent evaluator Result judge endpoint is required")
    for path in (artifact, trajectory, task_input, rubric, native, oracle):
        if not path.is_file():
            raise RuntimeError(f"missing semantic evidence: {path.name}")
    trace = json.loads(trajectory.read_text())
    answer = json.loads(artifact.read_text())
    metadata = trace.get("artifact") or {}
    absence = (isinstance(answer, dict) and answer.get("evidence_kind") == "evaluator_observed_absence"
               and answer.get("candidate_authored") is False and answer.get("artifact_present") is False)
    if absence:
        # Readiness judges the absence itself (evaluator-written observation, never
        # presented as Candidate-authored): only the case binding is checked, and the
        # trajectory must confirm that no artifact was authored.
        if trace.get("case_id") != case_id or answer.get("case_id") != case_id or metadata.get("path"):
            raise RuntimeError("absence observation/trajectory case binding mismatch")
    else:
        if trace.get("case_id") != case_id or trace.get("answer") != answer:
            raise RuntimeError("artifact/trajectory case binding mismatch")
        if not valid_agent_result(answer, case_id, expected_binding=trace.get("binding")):
            raise RuntimeError("malformed or unbound agent-authored artifact")
        if metadata.get("origin") != "lower_model_final_response" or metadata.get("evaluator_synthesized") is not False:
            raise RuntimeError("artifact does not originate from the model-driven product execution")
    command = [sys.executable, str(SHARED / "result_judge.py"), "--case-id", case_id,
        "--task-input", str(task_input), "--rubric", str(rubric),
        "--rubric-dimensions", str(RESULT_DIMENSIONS), "--agent-artifact", str(artifact),
        "--trajectory", str(trajectory), "--native-evidence", str(native), "--oracle-summary", str(oracle),
        "--broker-endpoint", endpoint, "--broker-placeholder", "broker-only-placeholder", "--output-dir", str(output)]
    caps_path = score_cap_contract(case_id=case_id, rubric=rubric, native=native,
                                   oracle=oracle, output=output)
    if caps_path is not None:
        command.extend(["--score-cap-contract", str(caps_path)])
    def rejudge():
        return subprocess.run(command, text=True, capture_output=True, check=False)

    process = rejudge()
    # result_score_caps.json (written above, inside `output`) is a caller input
    # and is restored into the fresh attempt by resample_refused_verdict.
    again = resample_refused_verdict(output, rejudge)
    if again is not None:
        process = again
    contract_path = output / "result_score_contract.json"
    if not contract_path.is_file():
        raise RuntimeError(f"Result judge produced no contract (exit {process.returncode})")
    contract = json.loads(contract_path.read_text())
    if (contract.get("contract_valid") is not True or contract.get("result_score_publishable") is not True
            or ((contract.get("provider_usage") or {}).get("completed_responses")
                != (2 if contract.get("early_stop_resample") else 1))):
        raise SemanticMeasurementUnavailable(contract_path, contract, process.returncode)
    return contract
