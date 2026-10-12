"""Task-specific trusted evidence adapter shared by dev and hidden scoring.

No semantic scores are computed here. Product-generated scientific files and
the private scientific reference are supplied to the independent Result judge.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
from case_world import has_case_world
from lower_agent_launcher import (ModelContentError, RELEASE_ARTIFACTS, _sanitized,
                                  validate_authored_artifact, validate_author_response_origin)

# D23 malformed artifact: the ModelContentError texts that validate_authored_artifact
# raises purely out of the Candidate's own agent_result.json bytes -- the disclosed
# contract's required fields, their types, the case-local case_id and an artifact that
# claims evaluator ownership.  Every other ModelContentError it can raise compares the
# artifact against evaluator-observed digests or captures and is deliberately absent.
CANDIDATE_ARTIFACT_FINDINGS = frozenset({
    "lower model artifact must be an object",
    "lower model artifact is missing required schema fields",
    "lower model artifact case_id mismatch",
    "lower model artifact contains evaluator source marker",
    "lower model artifact tool_events must be an array",
    "lower model artifact observed_facts must be an object",
    "lower model artifact receipt_id must be a nonempty string",
    "lower model artifact state must be a nonempty string",
    "lower model artifact honest_recovery must be boolean",
    "lower model artifact safe must be boolean",
    "lower model artifact oracle_leak must be boolean",
})
CANDIDATE_ARTIFACT_FATAL_CLASS = "candidate_artifact_failure"
from protocol import write_json, tree_digest, canonical_json


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_immutable_evidence(output: Path, documents: dict) -> None:
    """Preflight the entire evidence bundle before writing any missing file."""
    encoded = {name: canonical_json(value) for name, value in documents.items()}
    for name, payload in encoded.items():
        path = output / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != payload)):
            raise ValueError(f"Evidence changed; use a new scoring directory: {name}")
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in encoded.items():
        path = output / name
        if path.exists():
            continue
        try:
            with path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise ValueError(f"Concurrent evidence change; use a new scoring directory: {name}")


def scientific_spec(case_id: str):
    spec = importlib.util.spec_from_file_location("agentswe_ai_science_reference", ROOT / "evaluator/harness/oracles.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SPECS[case_id]


def scientific_reference(case_id: str) -> dict:
    return asdict(scientific_spec(case_id))


# D23 missing artifact: the launcher has already sorted the infrastructure outcomes out
# before a record can reach `candidate_behavior_failure`
# (lower_agent_launcher.py:1755-1769), and every field read below is written by the
# trusted launcher, never copied from the Candidate.  A finding is returned only when the
# evaluator's own record says the apparatus was healthy and the PRODUCT ended the case.
MISSING_ARTIFACT_FATAL_CLASS = "candidate_artifact_failure"
MISSING_ARTIFACT_CANDIDATE_CLASSES = frozenset({
    "candidate_behavior_failure", "candidate_product_failure",
    "candidate_policy_violation",
})


def candidate_missing_artifact_finding(record: dict) -> str | None:
    """Why the Candidate has no artifact, or None to keep today's behaviour verbatim."""
    if (record.get("classification") not in MISSING_ARTIFACT_CANDIDATE_CLASSES
            or record.get("classification_axis") != "candidate"
            or record.get("real_execution") is not True
            or record.get("infrastructure_invalid") is True
            or record.get("failure_attribution")):
        # `failure_attribution` present means the launcher already booked an attributed
        # outcome of its own (_d14_candidate_timeout, lower_agent_launcher.py:1909-1941).
        # That record reaches the shared fatal gate unaided and must not be rewritten.
        return None
    if record.get("agent_artifact_path") or record.get("agent_artifact_sha256"):
        # The record claims an artifact the disk does not have: evaluator-owned evidence
        # went missing between execution and finalization.  Not a Candidate fault.
        return None
    broker = record.get("broker")
    if not isinstance(broker, dict):
        return None

    def count(key):
        try:
            return int(broker.get(key, 0) or 0)
        except (TypeError, ValueError):
            return -1

    if (broker.get("transport_error") or broker.get("before_error") or broker.get("after_error")
            or broker.get("budget_exceeded") is True
            or broker.get("usage_complete") is not True
            or count("successful_calls") <= 0
            or count("failures_delta") != 0
            or count("usage_unknown_calls_delta") != 0
            or count("unattributed_usage_unknown_calls") != 0
            or count("deadline_aborted_calls_delta") != 0):
        # Anything the evaluator's own broker could not settle -- including a row this
        # broker cut at the absolute case deadline -- stays unresolved, as today.
        return None
    loop = record.get("action_loop") if isinstance(record.get("action_loop"), dict) else {}
    if (record.get("authoring_failure_axis") == "infrastructure"
            or loop.get("failure_axis") == "infrastructure"):
        return None
    if record.get("authoring_error"):
        return ("the lower model's final authoring response did not produce a usable "
                "agent_result.json: " + str(record["authoring_error"]))
    if loop.get("failure_axis") == "candidate" and loop.get("error"):
        # CaseBudgetReserveReached is deliberately a ModelContentError: under D14 an
        # exhausted case budget is a Candidate outcome (lower_agent_launcher.py:129-136).
        return ("the product action loop ended on the Candidate axis before any result "
                "was authored: " + str(loop["error"]))
    if loop.get("model_finished") is False and record.get("authoring_call_completed") is False:
        return ("the lower model never finished the action loop, so the evaluator never "
                "made the final authoring call")
    return None


def prepare_case_evidence(*, case_id: str, record_path: Path, candidate: Path,
                          candidate_digest: str, output: Path) -> dict:
    """Validate immutable product evidence and materialize sanitized inputs."""
    record_path, candidate, output = Path(record_path), Path(candidate), Path(output)
    record = read(record_path)
    base = record_path.parent
    if record.get("case_id") != case_id:
        raise ValueError("launcher record case mismatch")
    if tree_digest(candidate) != candidate_digest:
        raise ValueError("Candidate source changed before evidence preparation")
    integrity = record.get("integrity", {})
    if integrity.get("candidate_repo_digest") != candidate_digest:
        raise ValueError("launcher Candidate digest does not match frozen source")
    task = ROOT / "agentloop/cases" / case_id / "task.md"
    if record.get("task_sha256") != sha(task):
        raise ValueError("case task changed since execution")
    if record.get('execution_stage') == 'build':
        from build_evidence import validate_build_record
        validate_build_record(record, candidate=candidate, case_id=case_id, task=task)
        write_immutable_evidence(output, {'execution_record.json': record})
        # Compilation was actually attempted; no lower rollout/artifact exists.
        # The shared fatal gate validates the causal evidence before issuing 0.
        return {'case_input': task, 'rubric': ROOT / 'agentloop/result_rubric.md',
                'artifact': None, 'raw_trajectory': None, 'native_evidence': None,
                'private_oracle': None, 'execution_record': record,
                'candidate_digest': candidate_digest, 'case_id': case_id}
    trajectory = base / "raw_action_trajectory.json"
    raw = read(trajectory)
    if raw.get("case_id") != case_id:
        raise ValueError("raw product trajectory case mismatch")
    if record.get("action_loop", {}).get("raw_trajectory_sha256") != sha(trajectory):
        raise ValueError("raw product trajectory changed since launch")
    events = raw.get("events", [])
    hashes = {name: sha(base / name) for name in RELEASE_ARTIFACTS if (base / name).is_file()}
    if hashes != integrity.get("artifact_hashes"):
        raise ValueError("product delivery files changed after launch")
    artifact = base / "agent_result.json"
    record = dict(record)
    record["candidate_digest"] = candidate_digest
    record["execution_attempted"] = bool(record.get("real_execution"))
    record["artifact_validation"] = {"validated_by": "evaluator", "valid": False}
    if record.get("classification_axis") == "infrastructure":
        record["infrastructure_invalid"] = True
    author_claim_mismatches = []
    if artifact.is_file():
        try:
            parsed_artifact = read(artifact)
        except ValueError as exc:
            # D23 malformed artifact: the Candidate's own bytes do not parse.  Naming it
            # here keeps it out of the evaluator-side "evidence_unresolved" stub.
            parsed_artifact = None
            artifact_finding = "lower model artifact is not parseable JSON: " + str(exc)
        else:
            artifact_finding = None
        if parsed_artifact is not None:
            validate_author_response_origin(parsed_artifact, record, base)
            try:
                author_claim_mismatches = validate_authored_artifact(
                    parsed_artifact, case_id=case_id,
                    rollout_digest=integrity["rollout_digest"], hashes=hashes,
                    trajectory=events, trajectory_digest=integrity["trajectory_digest"],
                    runtime_digest=integrity["runtime_case_digest"],
                    case_world_digest=integrity.get("case_world_digest"),
                )
            except ModelContentError as exc:
                # D23 malformed artifact: only the contract findings that are read out of
                # the artifact itself become this case's Candidate zero.  Anything else
                # (origin, rollout/release digests, empty trajectory) propagates unchanged.
                if str(exc) not in CANDIDATE_ARTIFACT_FINDINGS:
                    raise
                artifact_finding = str(exc)
        if artifact_finding is not None:
            # The zero must still be bound to evidence the evaluator owns: the stored
            # artifact digest, a real execution and a healthy evaluator-owned broker.
            # Without all three this stays exactly as unresolved as it is today.
            broker = record.get("broker") or {}
            if (record.get("agent_artifact_sha256") != sha(artifact)
                    or record.get("real_execution") is not True
                    or int(broker.get("successful_calls", 0) or 0) <= 0):
                raise ValueError("model-authored artifact changed after launch"
                                 if record.get("agent_artifact_sha256") != sha(artifact)
                                 else "contract-violating artifact lacks a healthy observed execution")
            record.update(classification=CANDIDATE_ARTIFACT_FATAL_CLASS,
                classification_axis="candidate", execution_attempted=True,
                environment_preflight={"valid": True,
                    "scope": "owned case scope and evaluator-owned broker"},
                failure_attribution={
                    "party": "candidate", "observed_by": "evaluator", "fatal": True,
                    "reason": "The product-authored agent_result.json violates the disclosed"
                              " contract: " + artifact_finding + ".",
                    "evidence_paths": [str(artifact.resolve()), str(record_path.resolve())]})
            write_immutable_evidence(output, {"execution_record.json": record})
            return {"case_input": task, "rubric": ROOT / "agentloop/result_rubric.md",
                    "artifact": None, "raw_trajectory": None, "native_evidence": None,
                    "private_oracle": None, "execution_record": record,
                    "candidate_digest": candidate_digest, "case_id": case_id}
        if record.get("agent_artifact_sha256") != sha(artifact):
            raise ValueError("model-authored artifact changed after launch")
        record["artifact_validation"].update(valid=True, sha256=sha(artifact))
    elif case_id.startswith("test_"):
        # D23 missing artifact: the product delivered nothing for this hidden case.
        # Today this function still succeeds with artifact=None, ai_shared_finalize.py:
        # 91-97 hands the None on, and formal_axes_shared.py:749-754 turns it into
        # "<case>: missing lower-agent evidence: artifact", which voids the Result axis
        # for all six cases at :936.  When the evaluator's own launcher record says the
        # product itself ended the case, this is that case's Candidate zero.
        missing_finding = candidate_missing_artifact_finding(record)
        if missing_finding is not None:
            evidence_paths = [str(record_path.resolve()), str(trajectory.resolve())]
            guard = base / "dispatch_guard.json"
            if guard.is_file():
                evidence_paths.append(str(guard.resolve()))
            successful = int((record.get("broker") or {}).get("successful_calls", 0) or 0)
            record.update(classification=MISSING_ARTIFACT_FATAL_CLASS,
                classification_axis="candidate", execution_attempted=True,
                environment_preflight={"valid": True,
                    "scope": "owned case scope and evaluator-owned broker"},
                failure_attribution={
                    "party": "candidate", "observed_by": "evaluator", "fatal": True,
                    "reason": "The product delivered no agent_result.json for this case;"
                              " the evaluator observed " + str(successful) + " successful"
                              " lower-model call(s) and " + missing_finding + ".",
                    "evidence_paths": evidence_paths})
            write_immutable_evidence(output, {"execution_record.json": record})
            return {"case_input": task, "rubric": ROOT / "agentloop/result_rubric.md",
                    "artifact": None, "raw_trajectory": None, "native_evidence": None,
                    "private_oracle": None, "execution_record": record,
                    "candidate_digest": candidate_digest, "case_id": case_id}
    output.mkdir(parents=True, exist_ok=True)
    scientific_files = {}
    for name in RELEASE_ARTIFACTS:
        path = base / name
        if not path.is_file() or path.suffix == ".zip":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 100_000:
            raise ValueError(f"oversized scientific evidence requires explicit packing: {name}")
        scientific_files[name] = read(path) if path.suffix == ".json" else text
    native = {"case_id": case_id, "classification": record.get("classification"),
              "product_entry": record.get("entrypoint"),
              "scientific_product_files": _sanitized(scientific_files),
              "artifact_hashes": hashes, "runtime_case": record.get("runtime_case"),
              "model_author_claim_comparisons": _sanitized(author_claim_mismatches),
              "author_claims_preserved_verbatim": True,
              "observed_operations": [event.get("operation") for event in events]}
    from scientific_audit import collect_scientific_audit
    scientific_audit = collect_scientific_audit(base=base, events=events, spec=scientific_spec(case_id))
    native["scientific_audit_bundles"] = _sanitized(scientific_audit["bundles"])
    oracle = {"case_id": case_id, "scientific_reference": scientific_reference(case_id),
              "decision_axes": {"scientific_decision": "block prohibits paper publication/scientific review approval",
                                "governed_archive_commit": "may commit a byte-bound audit capsule preserving block; never upgrades it to release"},
              "reference_is_evaluator_only": True, "automatically_computed_score": None}
    oracle["result_score_caps"] = _sanitized(scientific_audit["result_score_caps"])
    world_path = base / "case_world.json"
    if world_path.is_file():
        world = read(world_path)
        world_hash = world.pop("world_digest", None)
        calculated = hashlib.sha256(json.dumps(world, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if world_hash != calculated or world_hash != integrity.get("case_world_digest"):
            raise ValueError("case-world evidence integrity mismatch")
        comparisons = []
        for attempt in world.get("attempts", []):
            evidence = base / attempt["evidence_path"]
            if base.resolve() not in evidence.resolve().parents or sha(evidence) != attempt["evidence_sha256"]:
                raise ValueError("case-world comparison evidence changed")
            comparison = read(evidence)
            comparisons.append({key: comparison.get(key) for key in (
                "transition", "operation", "checks", "advanced", "mutation", "evidence")})
        oracle["case_recovery_comparisons"] = _sanitized(comparisons)
        oracle["all_required_incidents_observed"] = world.get("complete")
        history = world.get("initial_history")
        if history:
            history_path = base / history["evidence_path"]
            if base.resolve() not in history_path.resolve().parents or sha(history_path) != history["evidence_sha256"]:
                raise ValueError("initial historical environment evidence changed")
            oracle["evaluator_owned_initial_history_not_model_actions"] = _sanitized(read(history_path))
    elif case_id.startswith("test_"):
        raise ValueError("hidden case-world comparison evidence is missing")
    elif has_case_world(case_id) and integrity.get("case_world_digest"):
        # 2026-09-21: the public dev cases have a case world too.  If the launcher
        # bound a world digest for this case, the world file must be here; silently
        # falling back to three ``unavailable`` ceilings would hand the dev judge a
        # weaker contract than the hidden judge gets for the same product.
        raise ValueError("public dev case-world comparison evidence is missing")
    score_cap = output / "score_cap_contract.json"
    cap_document = {"schema_version": "agentswe-result-score-caps/v1",
        "case_id": case_id, "rubric_sha256": sha(ROOT / "agentloop/result_rubric.md"),
        "native_evidence_sha256": hashlib.sha256(canonical_json(native)).hexdigest(),
        "oracle_summary_sha256": hashlib.sha256(canonical_json(oracle)).hexdigest(),
        "entries": scientific_audit["result_score_caps"]}
    write_immutable_evidence(output, {"native_evidence.json": native,
        "oracle_summary.json": oracle, "execution_record.json": record,
        "score_cap_contract.json": cap_document})
    return {"case_input": task, "rubric": ROOT / "agentloop/result_rubric.md",
            "artifact": artifact if artifact.is_file() else None,
            "raw_trajectory": trajectory,
            "native_evidence": output / "native_evidence.json",
            "private_oracle": output / "oracle_summary.json",
            "execution_record": record, "candidate_digest": candidate_digest,
            "score_cap_contract": score_cap,
            "case_id": case_id}


# --- judge resample (2026-09-20) ---------------------------------------------
# A shared Result judge that completed one paid-for response and then refused to
# publish it is an evaluator-side sampling fault, not a broken apparatus and not
# a Candidate fault.  Without this, two_round_controller.py:418-421 turns any
# ``contract_valid is not True`` into ``classification_axis == 'infrastructure'``,
# which _dev_gate (:471) and Controller.submit (:943-948) escalate to
# ``public_infrastructure_invalid`` plus a terminal latch with
# retry_allowed=false -- one malformed judge envelope ends the whole formal run.
# The hidden axis already recovers by setting the refused attempt aside and
# judging again; this does the same for the public round, exactly once.
RESAMPLEABLE_JUDGE_STATES = {"model_output_invalid"}
JUDGE_RESAMPLE_ATTEMPTS = 1


def refused_judge_state(result: dict) -> str | None:
    """The judge's own evaluation_state when it answered but refused to publish.

    None -- keep today's terminal behaviour -- unless the judge's own contract
    proves the apparatus worked: one completed response, complete usage
    accounting, and a refusal that is about the model's output rather than the
    infrastructure.  A missing or unreadable contract, a transport failure and
    ``infrastructure_error`` all return None and stay terminal.
    """
    if result.get("contract_valid"):
        return None
    contract_path = result.get("contract_path")
    if not contract_path:
        return None
    try:
        contract = read(Path(contract_path))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict):
        return None
    usage = contract.get("provider_usage")
    if not isinstance(usage, dict):
        return None
    if usage.get("completed_responses") != 1 or usage.get("usage_complete") is not True:
        return None
    state = contract.get("evaluation_state")
    return state if state in RESAMPLEABLE_JUDGE_STATES else None


def resample_refused_verdict(result: dict, *, evidence: dict, judge_dir: Path,
                             broker_endpoint: str, judge) -> dict:
    """Ask the evaluator's own judge again, in a new immutable directory.

    The Candidate product is not re-executed and no lower-agent call is
    re-issued: ``evidence`` is the same immutable input manifest, reused byte
    for byte.  The refused attempt is renamed, never deleted, so both verdicts
    stay auditable and the control plane's one-response-per-directory contract
    is preserved.
    """
    for attempt in range(1, JUDGE_RESAMPLE_ATTEMPTS + 1):
        state = refused_judge_state(result)
        if state is None:
            return result
        retired = judge_dir.parent / f"judge.attempt-{attempt:03d}-{state}"
        if retired.exists() or not judge_dir.is_dir():
            return result
        judge_dir.rename(retired)
        write_json(judge_dir.parent / f"judge_resample_{attempt:03d}.json", {
            "schema_version": "agentswe-ai-scientist-judge-resample-v1",
            "attempt": attempt,
            "retired_attempt": str(retired),
            "retired_evaluation_state": state,
            "candidate_product_re_executed": False,
            "lower_agent_calls_re_issued": 0,
            "reason": "the shared Result judge completed one response and refused to "
                      "publish it; the evaluator's own judge is resampled into a new "
                      "immutable directory and the refused attempt is retained",
        })
        result = judge(**evidence, output=judge_dir, broker_endpoint=broker_endpoint)
    return result


def score_case(*, case_id: str, record_path: Path, candidate: Path,
               candidate_digest: str, output: Path, broker_endpoint: str) -> dict:
    sys.path.insert(0, "@@AGENTSWE_EDITING_CONTROL@@")
    from execution_scoring import judge_execution_case
    evidence = prepare_case_evidence(case_id=case_id, record_path=record_path,
                                    candidate=candidate, candidate_digest=candidate_digest,
                                    output=Path(output) / "inputs")
    judge_dir = Path(output) / "judge"
    result = judge_execution_case(**evidence, output=judge_dir,
                                  broker_endpoint=broker_endpoint)
    result = resample_refused_verdict(result, evidence=evidence, judge_dir=judge_dir,
                                      broker_endpoint=broker_endpoint,
                                      judge=judge_execution_case)
    if result.get("contract_valid") and result.get("contract_path"):
        contract = read(Path(result["contract_path"]))
        dimensions = read(ROOT / "evaluator/result_dimensions.json")
        # ``score_cap``/``score_cap_conditions`` are the evidence-bound ceiling
        # contract this evaluator handed the judge, and ``errors`` are the judge's
        # own harness/validator findings.  Both were computed on the dev path and
        # then dropped here, which is why dev feedback could never name the cap that
        # decides the hidden score.  They are this case's OWN published cap entries:
        # no hidden-case evidence and no private oracle expectation is added.
        result["feedback"] = {key: contract[key] for key in (
            "result_score", "assessment", "major_errors", "dimensions", "ceiling_assessments",
            "score_cap", "score_cap_conditions", "errors", *dimensions,
        ) if key in contract}
        if result.get('classification') == 'candidate_zero':
            result['feedback']['assessment'] = contract.get('reason')
    return result
