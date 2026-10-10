"""Trusted OpenHands evidence adapter shared by dev and hidden Result."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lower_agent"))
import openhands_lower_agent as lower
from openhands_lower_agent import tree_digest, validate_trajectory_binding
from model_origin import completed_origin


def read(path): return json.loads(Path(path).read_text())
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def immutable_bundle(output, documents):
    encoded = {name: (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode() for name, value in documents.items()}
    for name, data in encoded.items():
        path = output / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != data)):
            raise ValueError(f"OpenHands evidence changed; use a new score directory: {name}")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in encoded.items():
        path = output / name
        if not path.exists():
            with path.open("xb") as handle: handle.write(data)


# D23 malformed artifact: the fatal Candidate class the evaluator-owned
# execution_contract.FATAL_CANDIDATE_CLASSES set already recognises for a product whose
# delivered artifact violates the disclosed contract.
CANDIDATE_ARTIFACT_FATAL_CLASS = "candidate_artifact_failure"


def model_value(path):
    text = Path(path).read_text().strip()
    if text.startswith("```"): text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict): raise ValueError("completed model response is not a JSON object")
    return value


def prepare_case_evidence(*, case_id, record_path, candidate, candidate_digest, output, historical_origin=None):
    record_path, output = Path(record_path), Path(output)
    base = record_path.parent
    record, launcher = read(record_path), read(base / "launcher_result.json")
    # Supervisor hard-kill at the case budget: the worker never wrote its launcher summary,
    # so the fallback carries no identity/capture digests. The controller (evaluator) already
    # attributed the failure to the Candidate with a fatal gate; identity comes from the
    # controller record and the frozen candidate tree, never from the Candidate runtime.
    attribution = record.get("failure_attribution") or {}
    deadline_killed = (launcher.get("case_id") is None and launcher.get("exit_code") in {124, 125}
                       and record.get("classification") == "candidate_product_failure"
                       and attribution.get("party") == "candidate" and attribution.get("observed_by") == "evaluator"
                       and attribution.get("fatal") is True)
    if record.get("case_id") != case_id or (launcher.get("case_id") != case_id and not deadline_killed):
        raise ValueError("OpenHands case identity mismatch")
    if tree_digest(Path(candidate)) != candidate_digest or (launcher.get("candidate_source_digest") != candidate_digest and not deadline_killed):
        raise ValueError("OpenHands frozen Candidate digest mismatch")
    group = "dev_cases" if case_id.startswith("dev_") else "test_cases"
    root = ROOT / group / case_id
    task = root / "natural_task.md" if (root / "natural_task.md").is_file() else root / "input.md"
    if launcher.get("task_sha256") != sha(task) and not deadline_killed: raise ValueError("OpenHands runtime task changed")
    fixtures = root / "assets/fixtures.json"
    if not fixtures.is_file(): fixtures = root / "assets/scenario.json"
    if fixtures.is_file() and launcher.get("fixture_sha256") != sha(fixtures) and not deadline_killed:
        raise ValueError("OpenHands active case fixtures changed")
    artifact, trajectory, world = base / "agent_result.json", base / "trajectory.json", base / "case_world.json"
    preflight, product_state = base / "environment_preflight.json", base / "final_product_state.json"
    for name, path in (("agent_artifact_sha256", artifact), ("trajectory_sha256", trajectory), ("case_world_sha256", world),
                       ("model_final_response_sha256", base / "model_final_response.txt"),
                       ("environment_preflight_sha256", preflight), ("final_product_state_sha256", product_state)):
        if deadline_killed and launcher.get(name) is None:
            continue  # worker never reached its capture summary; the files stay raw evidence, not claims
        if (path.is_file() and launcher.get(name) != sha(path)) or (launcher.get(name) and not path.is_file()):
            raise ValueError(f"OpenHands missing or changed captured {name}")
    binding = None
    raw = read(trajectory) if trajectory.is_file() else {}
    for event in raw.get("steps", []):
        captured = base / f"model_action_{event['step']:03d}_response.txt"
        if not captured.is_file() or sha(captured) != event.get("model_response_sha256"):
            raise ValueError("OpenHands action lacks original captured model decision")
        choice = model_value(captured)
        if choice.get("action", "") != event.get("action"):
            raise ValueError("OpenHands captured model selected a different action")
    # A format-repair turn republished the parsed reply as the canonical capture and
    # kept the unparsed original beside it. The canonical bytes are already covered by
    # every check above; reconcile the claim itself so the extra file stays bound too.
    for capture, repair in ([(base / "model_final_response.txt", launcher.get("format_repair"))]
                            + [(base / f"model_action_{event['step']:03d}_response.txt", event.get("format_repair"))
                               for event in raw.get("steps", [])]):
        if not repair or not repair.get("repaired_sha256"):
            continue  # no repair here, or the repair failed too and the case is already an infrastructure failure
        original = capture.with_name(capture.stem + ".original" + capture.suffix)
        if repair.get("attempted") is not True or sha(capture) != repair["repaired_sha256"]:
            raise ValueError("OpenHands format repair does not match the canonical captured response")
        if not original.is_file() or sha(original) != repair.get("original_sha256"):
            raise ValueError("OpenHands format repair lacks the original unparsed capture")
    facts = read(world) if world.is_file() else {}
    if facts and (facts.get("case_id") != case_id or "comparisons" not in facts or "observed" not in facts):
        raise ValueError("OpenHands case-world comparisons are missing")
    attempted = launcher.get("real_execution") is True
    if deadline_killed:
        attempted = any(step.get("attempted") is True for step in raw.get("steps", []))
        record = dict(record, deadline_killed=True)
    record = dict(record, candidate_digest=candidate_digest, real_execution=attempted, execution_attempted=attempted)
    record["artifact_validation"] = {"validated_by": "evaluator", "valid": False}
    record["environment_preflight"] = read(preflight) if preflight.is_file() else {"valid": False}
    if record.get("classification") in {"provider_failure", "broker_failure", "lower_agent_infrastructure_failure"}:
        record["infrastructure_invalid"] = True
    if historical_origin is not None and not artifact.is_file():
        artifact = base / "model_final_response.txt"
    if artifact.is_file():
        # D23 malformed artifact: these are the Candidate's own delivered bytes, already
        # proven unchanged since capture by the launcher digest checks above.  Bytes that
        # do not parse, or that are not a JSON object, are a product delivery defect for
        # THIS case and must not be laundered into an evaluator-side evidence failure that
        # voids the whole Result axis.
        artifact_finding = None
        try:
            authored = model_value(base / "model_final_response.txt")
            persisted = read(artifact)
        except ValueError as exc:
            authored = persisted = None
            artifact_finding = "product-authored final response is not a contract-shaped JSON object: " + str(exc)
        if artifact_finding is None:
            try:
                model_value(artifact)
            except ValueError as exc:
                artifact_finding = "product-authored agent_result.json is not a contract-shaped JSON object: " + str(exc)
        if artifact_finding is not None:
            preflight_valid = (record.get("environment_preflight") or {}).get("valid") is True
            if (not attempted or not preflight_valid
                    or record.get("infrastructure_invalid") is True):
                raise ValueError(artifact_finding)
            record.update(classification=CANDIDATE_ARTIFACT_FATAL_CLASS, failure_attribution={
                "party": "candidate", "observed_by": "evaluator", "fatal": True,
                "reason": "The product-authored final result violates the disclosed"
                          " agent_result.json contract: " + artifact_finding + ".",
                "evidence_paths": [str(artifact.resolve()), str(record_path.resolve())]})
            immutable_bundle(output, {"execution_record.json": record})
            return {"case_input": task, "rubric": ROOT / "evaluator/result_rubric.md",
                    "artifact": None, "raw_trajectory": None, "native_evidence": None,
                    "private_oracle": None, "execution_record": record,
                    "candidate_digest": candidate_digest, "case_id": case_id}
        if authored.get("artifact", authored) != persisted:
            raise ValueError("OpenHands persisted artifact differs from captured completed model response")
        origin = completed_origin(lower, base, case_id, model_value(artifact), historical_proof=historical_origin)
        binding = validate_trajectory_binding(base, case_id, model_value(artifact), trusted_origin=origin)
        if not binding.get("bound"):
            raise ValueError("OpenHands artifact does not match product trajectory")
        if (historical_origin is None and launcher.get("execution_phase", {}).get("phase") != "complete") or launcher.get("execution_phase", {}).get("response_sha256") != sha(base / "model_final_response.txt"):
            raise ValueError("OpenHands final model authoring completion is unproven")
        if not facts or not product_state.is_file():
            raise ValueError("OpenHands successful artifact lacks actual world/product observations")
        record["artifact_validation"].update(valid=True, sha256=sha(artifact), captured_origin=origin)
        if historical_origin is not None:
            record.update(classification="candidate_behavior_failure", failure_attribution={"party":"ungraded_behavior", "reason":"Original completed model response has independently verified request/product provenance; semantic claims remain ungraded."})
            record["measurement_limitations"] = read(historical_origin).get("measurement_limitations", [])
    elif launcher.get("execution_phase", {}).get("phase") in {"artifact_validation", "model_choice_validation"}:
        record.update(infrastructure_invalid=True, failure_attribution={"party":"evaluator", "reason":"Completed lower response could not be bound by the evaluator protocol; no Candidate fatal score inferred."})
    record["broker"] = dict(record.get("broker_delta") or {})
    native = {"case_id": case_id, "product_execution": launcher.get("product_attestation"),
              "execution_phase": launcher.get("execution_phase"),
              "original_model_decisions": [{"step": event["step"], "choice": model_value(base / f"model_action_{event['step']:03d}_response.txt")} for event in raw.get("steps", [])],
              "measurement_limitations": record.get("measurement_limitations", []),
              "observed_product_state": facts.get("observed"), "typed_trajectory_binding": binding,
              "final_product_state": read(product_state) if product_state.is_file() else None}
    # Raw storage values are private runtime evidence, not useful judge claims.
    if isinstance(native["final_product_state"], dict): native["final_product_state"].pop("storage", None)
    oracle = {"case_id": case_id, "inputs": facts.get("inputs"), "comparisons": facts.get("comparisons"),
              "workspace_crash_coverage": facts.get("workspace_crash_coverage"),
              "fixture_digest": facts.get("fixture_digest"), "automatically_computed_score": None}
    immutable_bundle(output, {"native_evidence.json": native, "oracle_summary.json": oracle, "execution_record.json": record})
    return {"case_input": task, "rubric": ROOT / "evaluator/result_rubric.md",
            "artifact": artifact if artifact.is_file() else None, "raw_trajectory": trajectory if trajectory.is_file() else None,
            "native_evidence": output / "native_evidence.json", "private_oracle": output / "oracle_summary.json",
            "execution_record": record, "candidate_digest": candidate_digest, "case_id": case_id}


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


def score_case(*, case_id, record_path, candidate, candidate_digest, output, broker_endpoint, historical_origin=None):
    sys.path.insert(0, "@@AGENTSWE_EDITING_CONTROL@@")
    from execution_scoring import judge_execution_case
    evidence = prepare_case_evidence(case_id=case_id, record_path=record_path, candidate=candidate,
                                    candidate_digest=candidate_digest, output=Path(output) / "inputs", historical_origin=historical_origin)
    judge_dir = Path(output) / "judge"

    def rejudge():
        return judge_execution_case(**evidence, output=judge_dir, broker_endpoint=broker_endpoint)

    result = rejudge()
    again = resample_refused_verdict(judge_dir, rejudge)
    if again is not None:
        result = again
    if result.get("contract_valid") and result.get("contract_path"):
        contract = read(result["contract_path"])
        result["feedback"] = {key: contract[key] for key in ("result_score", "assessment", "major_errors", "dimensions", *read(ROOT / "evaluator/result_dimensions.json")) if key in contract}
        if contract.get("classification") == "candidate_zero":
            result["feedback"].setdefault("assessment", "Fatal Candidate failure on this case: " + str(contract.get("reason")))
            result["feedback"].setdefault("major_errors", [str(contract.get("reason"))])
    return result
