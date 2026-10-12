#!/usr/bin/env python3
"""Per-case Result-judge repair for the AgentSWE Edit formal path.

Repairs exactly one hidden case whose shared Result judge produced a
SCHEMA-INVALID answer over a SUCCESSFUL provider call, and nothing else.

Nothing under @@AGENTSWE_LEGACY_HOME@@ is written.  The shared judge protocol
(result_judge.py, judge_broker_runtime.py, formal_axes_shared.py,
execution_scoring.py) is used exactly as the one-stop uses it; this tool only

  1. proves the failure is an output-format failure over an HTTP 200
     `completed` provider response,
  2. proves the immutable scoring inputs still hash to the identity the
     original scoring_intent.json was bound to (so the repair judging sees
     byte-identical inputs),
  3. archives the invalid attempt directory (move, bytes preserved),
  4. starts a fresh evaluator-owned judge broker with the run's own broker
     configuration and pinned protocol, re-runs the tree's finalizer -- which
     issues ONE new logical request for the archived case and reuses every
     other case's bound contract -- and stops the broker again,
  5. records both attempts' usage in formal_aggregation.json.

`--dry-run` stops after step 2 + a read-only finalizer rehearsal that makes
zero provider calls.

OpenClaw layout additions
-------------------------
Two minimal additions; every existing proof and refusal is kept, and on every
tree that keeps the judging evidence flat under `<case>/` the resolution below
returns exactly the paths this tool used before (`case_paths` self-check in the
package).

(a) PER-CASE EVIDENCE LAYOUT.  openclaw (tree 14) splits the case directory:
    the immutable staged inputs live in `<case>/inputs/` and the shared judge's
    own output -- contract, provider response(s), attempt ledger, prompt,
    input_manifest.json, model_response.json, result_eval_result.json -- lives
    in `<case>/judge/` (`semantic_finalize.py` passes `--output-dir <case>/judge`).
    Every other tree writes all of it flat under `<case>/`.  `case_paths()`
    resolves the two from the evidence on disk and the archive becomes
    `<case>/judge.attempt-001-invalid/` instead of `<case>.attempt-001-invalid/`,
    so `<case>/inputs/` is never moved out from under the identity that
    `input_manifest.json` binds.  Because this tree's finalizer DOES rebuild
    `<case>/inputs/` for the one case it re-judges, attempt 1's copies are
    preserved inside the archive as `inputs.attempt-001/`.

(b) `<dimension>.max must equal <N>` is an OUTPUT-FORMAT error.
    result_judge.validate_response:826 compares the model's `max` field against
    the maxima the EVALUATOR staged in `inputs/result_dimensions.json`.  That
    field is a verbatim echo of a constant the judge has no discretion over, so
    getting it wrong is a defect of the JSON envelope, not a judgement: it
    carries no score, no ceiling and no verdict.  (The per-dimension `.score`
    bound and the `result_score` total are checked separately and are NOT in
    this class; a judge-established ceiling stays in REFUSED_ERRORS.)
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never leave .pyc files in the registry-pinned shared tree

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edit_run_layouts import (  # noqa: E402
    CONTROL_PYTHON, RESULT_JUDGE, SHARED_ROOT, Refusal,
    broker_config, contract_snapshot, credential_from_launch, fail_fast_finalizer,
    finalizer_command, finalizer_family, finalizer_supports_output, find_launch_record,
    free_port, broker_counter, identity_document, read_json, regenerated_inputs, repairs_on_disk,
    endpoint_flag, finalizer_path, result_axis_root, run_finalizer, sha256_file,
    single_shot_finalizer, task_root_from_launch,
    utc_now, write_json)

OPERATOR_NOTE = "schema-invalid judge output; one repair judging"
TOOL_PATH = Path(__file__).resolve()

# ---------------------------------------------------------------------------
# Which judge validation errors are OUTPUT-FORMAT errors.
#
# Grounded in result_judge.validate_response (the only producer of
# result_score_contract.json "errors").  A repair judging is allowed only when
# every recorded error is a shape defect of the JSON envelope the model had to
# emit.  Anything that encodes a judgement (a ceiling the judge itself
# established, a fatal-failure rule, a usage/transport defect) is refused:
# re-asking would be score shopping, not repair.
# ---------------------------------------------------------------------------
PRIMARY_FORMAT_ERRORS = (
    "top-level keys mismatch: ",
    "dimension IDs do not match the task-local rubric",
    "major_errors must be an array of strings",
    "assessment must be non-empty",
    "ceiling_assessments must cover exactly the semantic-review conditions",
    "invalid semantic ceiling assessment: ",
)
# result_judge.validate_response:826 -- f"{name}.max must equal {maximum}", where
# `maximum` comes from the evaluator-staged inputs/result_dimensions.json.  The
# model has to echo that constant back; a wrong echo is an envelope defect.  It is
# deliberately NOT ".score must be integer 0..N" (a bound on the judgement) and
# NOT any REFUSED_ERRORS ceiling.
DIMENSION_MAX_ERROR = re.compile(r"^[A-Za-z0-9_]+\.max must equal \d+$")
CASCADE_FORMAT_ERRORS = (
    "dimensions must be an object",
    "invalid result_state",
    "result_score must equal component sum ",
)
CASCADE_FORMAT_SUFFIXES = (
    " must be an object",
    ".evidence must be non-empty",
)
CASCADE_FORMAT_CONTAINS = (" is missing required keys ",)
# result_judge.main's own `except Exception` branch records the failure as a
# single "Type: message" string and writes no result_eval_result.json.  Two of
# those exceptions are still OUTPUT-FORMAT failures -- the model's bytes are not
# the required envelope at all, so validate_response never got to run:
#   json.loads in validate_response      -> "JSONDecodeError: ..."
#   the object check in validate_response-> "ValueError: judge output must be one JSON object"
# Everything else that branch can record (transport, credential, score-cap,
# schema-disagreement, missing-input failures) is not a format defect.  These
# count as format errors only for a contract that already proves a completed
# provider response, which the caller checks separately.
PARSE_FAILURE_ERRORS = (
    "JSONDecodeError: ",
    "ValueError: judge output must be one JSON object",
)
REFUSED_ERRORS = (
    "case_id mismatch",
    "fatal_candidate_failure must have zero total",
    "result_score exceeds evidenced task-local ceiling",
    "result_score exceeds judge-established ceiling",
    "missing or inconsistent provider token usage",
)


def classify_errors(errors: list, model_output_invalid: bool = False,
                    raw_unparseable: bool = False) -> tuple[bool, list[str]]:
    """Return (output_format_only, offending errors).

    ``model_output_invalid`` says the contract itself records a completed
    provider response whose OUTPUT was rejected; only then can an
    exception-shaped parse failure be read as a format defect.
    """
    offending: list[str] = []
    primary = False
    if raw_unparseable and model_output_invalid:
        # The provider's own message bytes are not one JSON object (proved by the
        # caller from provider_response.json): every validation error the evaluator
        # recorded is a cascade of that envelope defect, except a REFUSED verdict.
        primary = True
        for item in errors:
            text = str(item)
            if any(text.startswith(prefix) for prefix in REFUSED_ERRORS):
                offending.append(text)
        return (not offending), offending
    for item in errors:
        text = str(item)
        if model_output_invalid and any(text.startswith(prefix) for prefix in PARSE_FAILURE_ERRORS):
            primary = True
            continue
        if any(text.startswith(prefix) for prefix in PRIMARY_FORMAT_ERRORS) \
                or DIMENSION_MAX_ERROR.match(text):
            primary = True
            continue
        if any(text.startswith(prefix) for prefix in CASCADE_FORMAT_ERRORS):
            continue
        if any(text.endswith(suffix) for suffix in CASCADE_FORMAT_SUFFIXES):
            continue
        if any(token in text for token in CASCADE_FORMAT_CONTAINS):
            continue
        offending.append(text)
    if not primary:
        offending.append("no key/dimension/major_errors/assessment/ceiling_assessments/"
                         "envelope-parse format error is recorded")
    return (not offending), offending


# ---- package 107: per-task per-case evidence layout
# ---------------------------------------------------------------------------
# Resolved from the bytes on disk, never from a task name, so a tree that moves
# its judging evidence cannot silently get the wrong answer.
#
#   flat        (nine trees)  <case>/result_score_contract.json  ...
#               archive       <case>.attempt-001-invalid/
#   judge_subdir (openclaw)   <case>/judge/result_score_contract.json  ...
#                             <case>/inputs/   (staged, NOT moved)
#               archive       <case>/judge.attempt-001-invalid/
#
# A case that has neither falls back to `flat`, so the "missing per-case judging
# evidence: <path>" refusal keeps naming the flat path on every tree that is not
# openclaw, byte for byte as before.
# ---------------------------------------------------------------------------
CONTRACT_NAME = "result_score_contract.json"


def case_paths(run_dir: Path, case: str) -> dict:
    case_dir = result_axis_root(run_dir) / case
    flat = {"layout": "flat", "case_dir": case_dir, "evidence_dir": case_dir,
            "archive_dir": case_dir.with_name(case + ".attempt-001-invalid")}
    if (case_dir / CONTRACT_NAME).is_file():
        return flat
    judge_dir = case_dir / "judge"
    if (judge_dir / CONTRACT_NAME).is_file():
        return {"layout": "judge_subdir", "case_dir": case_dir,
                "evidence_dir": judge_dir,
                "archive_dir": case_dir / "judge.attempt-001-invalid"}
    return flat


def contract_snapshot_layout(run_dir: Path) -> dict:
    """contract_snapshot(), but finding a contract under `judge/` too."""
    snapshot = dict(contract_snapshot(run_dir))
    root = result_axis_root(run_dir)
    if root.is_dir():
        for case_dir in sorted(p for p in root.glob("test_*") if p.is_dir()):
            if case_dir.name in snapshot:
                continue
            paths = case_paths(run_dir, case_dir.name)
            contract = paths["evidence_dir"] / CONTRACT_NAME
            if paths["layout"] != "flat" and contract.is_file():
                snapshot[case_dir.name] = sha256_file(contract)
    return dict(sorted(snapshot.items()))


def repairs_on_disk_layout(run_dir: Path) -> list:
    """repairs_on_disk(), with `case_contract_valid_now` read from the right dir.

    The record itself is always written to the CASE root (`<case>/rejudge_record.json`)
    in both layouts, which is what edit_run_layouts.repairs_on_disk globs; only the
    contract it re-derives the case-level truth from can be one level deeper.
    On a flat tree every value below is already what the shared helper computed.
    """
    blocks = []
    for block in repairs_on_disk(run_dir):
        contract = case_paths(run_dir, block["case_id"])["evidence_dir"] / CONTRACT_NAME
        if contract.is_file():
            try:
                value = read_json(contract)
            except (OSError, ValueError):
                value = {}
            block = {**block,
                     "case_contract_valid_now": value.get("contract_valid") is True,
                     "case_result_score_now": value.get("result_score")}
        blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------
def check_preconditions(run_dir: Path, case: str, allow_transport_failure: bool,
                        allow_regenerated_inputs: bool) -> dict:
    paths = case_paths(run_dir, case)
    case_dir = paths["case_dir"]
    evidence_dir = paths["evidence_dir"]
    archive_dir = paths["archive_dir"]
    contract_path = evidence_dir / CONTRACT_NAME
    attempts_path = evidence_dir / "provider_response-attempts.json"

    if not (run_dir / "formal_aggregation.json").is_file():
        raise Refusal("run has no formal_aggregation.json; nothing to repair")
    if not (run_dir / "cleanup_attestation.json").is_file():
        raise Refusal("run has no cleanup_attestation.json; it may still be in flight -- refusing")
    if archive_dir.exists():
        raise Refusal(f"{archive_dir} already exists; this case was already repaired once")
    for path in (contract_path, attempts_path):
        if not path.is_file():
            raise Refusal(f"missing per-case judging evidence: {path}")

    contract = read_json(contract_path)
    if contract.get("contract_valid") is True or contract.get("result_score_publishable") is True:
        raise Refusal(f"{case} already has a valid, publishable contract -- refusing to rejudge")
    if contract.get("schema_version") != "agentswe-edit-result-score-contract-v1":
        raise Refusal(f"{case} contract is not a shared Result-judge contract")
    errors = contract.get("errors") or []
    completed = int((contract.get("provider_usage") or {}).get("completed_responses") or 0)
    raw_unparseable = False
    try:
        provider = read_json(evidence_dir / "provider_response.json")
        texts = [c.get("text", "") for o in (provider.get("output") or []) if o.get("type") == "message"
                 for c in (o.get("content") or []) if isinstance(c, dict)]
        if len(texts) == 1 and texts[0].strip():
            try:
                parsed = json.loads(texts[0])
                raw_unparseable = not isinstance(parsed, dict)
            except ValueError:
                raw_unparseable = True
    except (OSError, ValueError):
        raw_unparseable = False
    if raw_unparseable:
        print("   provider message text is not one JSON object (envelope defect proved from provider_response.json)")
    # 2026-09-21: a "case_id mismatch" that is only the cascade of a MISSING top-level
    # case_id key (the model omitted the key entirely, so None != case_id) is an envelope
    # defect, not a misrouted judgement.  A present-but-different case_id stays refused.
    if any(str(e) == "case_id mismatch" for e in errors) and any(str(e).startswith("top-level keys mismatch: ") for e in errors):
        try:
            _model = read_json(evidence_dir / "model_response.json")
            if isinstance(_model, dict) and "case_id" not in _model:
                print("   case_id mismatch is a cascade of the missing top-level case_id key (model_response.json has no case_id); treated as an envelope defect")
                errors = [e for e in errors if str(e) != "case_id mismatch"]
        except (OSError, ValueError):
            pass
    format_only, offending = classify_errors(
        errors, contract.get("evaluation_state") == "model_output_invalid" and completed == 1,
        raw_unparseable=raw_unparseable)
    if not format_only:
        raise Refusal(
            "refusing: the recorded failure is not an OUTPUT-FORMAT failure.\n"
            "  offending errors: " + json.dumps(offending, ensure_ascii=False)
            + "\n  A low score, a ceiling violation, a usage defect or an infrastructure"
              " error is never repaired by re-asking the judge.")

    attempts = read_json(attempts_path)
    rows = attempts.get("attempts") or []
    transport_ok = (
        attempts.get("logical_requests") == 1 and len(rows) == 1
        and rows[0].get("http_status") == 200
        and rows[0].get("response_status") == "completed"
        and rows[0].get("usage_known") is True
        and rows[0].get("state") == "terminal"
        and rows[0].get("error_type") is None)
    if not transport_ok:
        message = ("PROVIDER TRANSPORT FAILURE: the original attempt is not a single completed "
                   "HTTP 200 response with known usage.\n  attempt ledger: "
                   + json.dumps(rows, ensure_ascii=False)[:800])
        if not allow_transport_failure:
            raise Refusal(message + "\n  Pass --allow-transport-failure to repair it anyway; "
                                    "a transport failure is never repaired silently.")
        print("WARNING: " + message)
        print("WARNING: proceeding only because --allow-transport-failure was given.")

    # Immutable-input identity.  The bound identity lives in scoring_intent.json
    # on the shared-main/semantic trees and only in the judge's own
    # input_manifest.json on the standalone trees; both record path + sha256 for
    # every input the judging consumed.
    kind, declared = identity_document(evidence_dir)
    if kind == "scoring_intent":
        if declared["model"] != "deepseek-flash" or declared["effort"] != "max":
            raise Refusal("scoring_intent is not bound to the pinned deepseek-flash/max protocol")
        if declared["judge_source_digest"] != sha256_file(RESULT_JUDGE):
            raise Refusal("shared result_judge.py changed since the original judging; "
                          "a repair judging would not be the same protocol")
    judge = contract.get("judge") or {}
    if judge.get("model") != "deepseek-flash" or judge.get("reasoning_effort") != "max":
        raise Refusal("the original contract is not from the pinned deepseek-flash/max judge")

    regenerated = regenerated_inputs(case_dir, declared)
    input_drift = []
    for name, entry in declared["inputs"].items():
        if name in regenerated:
            continue  # lives inside the archived dir; handled below
        path = Path(entry["path"])
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            input_drift.append(name)
    if input_drift:
        raise Refusal("immutable scoring inputs changed since the original judging: "
                      + ", ".join(sorted(input_drift)))

    # Inputs the finalizer rewrites inside the case dir before judging again.
    reproducible, unproven = [], []
    for name in regenerated:
        entry = declared["inputs"][name]
        if sha256_file(Path(entry["path"])) != entry["sha256"]:
            raise Refusal(f"case-local input {name} already drifted from the scoring identity")
        source = regenerated_source(run_dir, case, name)
        if source is not None and sha256_file(source) == entry["sha256"]:
            reproducible.append(name)
        else:
            unproven.append(name)
    if unproven and not allow_regenerated_inputs:
        raise Refusal(
            "these judging inputs live inside the case directory and this tool cannot prove the\n"
            "  finalizer will regenerate them byte-identically: " + ", ".join(unproven)
            + "\n  A repair judging would then be a re-judging under DIFFERENT inputs, not a\n"
              "  format repair.  Pass --allow-regenerated-inputs to accept that and have it\n"
              "  recorded in rejudge_record.json, after checking what the regenerator now emits.")

    return {"case_dir": case_dir, "evidence_dir": evidence_dir,
            "layout": paths["layout"],
            "archive_dir": archive_dir, "contract": contract,
            "attempts": attempts, "identity_kind": kind, "declared": declared,
            "errors": [str(item) for item in errors],
            "regenerated_inputs": regenerated, "reproducible_inputs": reproducible,
            "unproven_inputs": unproven,
            "identity_sha256": declared["identity_sha256"]}


def regenerated_source(run_dir: Path, case: str, name: str):
    """The run-local source a case-dir input is copied from, when there is one.

    Only the oracle comparison has one: the hidden attestation names it and
    binds its digest, so a byte-identical regeneration can be proven.  Score-cap
    contracts are rebuilt by tree code and have no such source.
    """
    if name != "oracle_summary":
        return None
    try:
        hidden = hidden_document_any(run_dir)
    except Refusal:
        return None

    def find(value):
        if isinstance(value, dict):
            if value.get("case_id") == case and "private_oracle_comparison_path" in value:
                return value
            for child in value.values():
                found = find(child)
                if found is not None:
                    return found
        if isinstance(value, list):
            for child in value:
                found = find(child)
                if found is not None:
                    return found
        return None

    record = find(hidden)
    if record is None:
        return None
    path = Path(record["private_oracle_comparison_path"])
    if not path.is_file() or sha256_file(path) != record.get("private_oracle_comparison_sha256"):
        return None
    return path


def hidden_document_any(run_dir: Path) -> dict:
    """Read whichever hidden-after-freeze attestation this tree writes."""
    names = ("hidden-after-freeze-attestation.json", "hidden_after_freeze_attestation.json")
    for parent in (run_dir / "hidden", run_dir / "lifecycle", run_dir,
                   run_dir / "hidden_after_freeze"):
        for name in names:
            path = parent / name
            if path.is_file():
                return read_json(path)
    raise Refusal("no hidden-after-freeze attestation found in this run")


def check_rehearsal(aggregation: dict, case: str, family: str) -> list[str]:
    """Everything except the target case must already validate."""
    problems = []
    reasons = aggregation.get("result_reasons")
    if reasons is None:
        reasons = [r for r in (aggregation.get("reasons") or []) if not r.startswith("Code: ")]
    allowed_prefix = case + ": "
    for reason in reasons:
        if reason.startswith(allowed_prefix) or reason.startswith("Result: " + allowed_prefix):
            continue
        if reason.startswith("Result judge broker request delta "):
            continue  # expected: the unrecoverable prior attempt is counted, no call is made
        problems.append("unrelated blocking reason: " + reason)
    if not any(allowed_prefix in r for r in reasons):
        problems.append(f"the finalizer no longer reports {case} as blocking; nothing to repair")
    if any("already belongs to different immutable inputs" in r for r in reasons):
        problems.append("scoring identity drift: the finalizer no longer recognizes these inputs")
    recoverable = any(("prior scoring attempt requires recovery" in r
                       or "did not produce a publishable semantic contract" in r
                       or "returned an invalid measurement" in r
                       or "Result judge contract invalid" in r) for r in reasons)
    if not recoverable:
        problems.append("the finalizer does not report the case as an unrecovered judging attempt")
    if family in ("shared_main", "semantic_finalize"):
        covered = set(aggregation.get("candidate_zero_cases") or []) | set(
            aggregation.get("semantically_judged_cases") or [])
        missing = [c for c in (aggregation.get("selected_cases") or [])
                   if c != case and c not in covered]
        if missing:
            problems.append("other cases are not resolved either: " + ", ".join(missing))
    if aggregation.get("code_reasons"):
        problems.append("Code axis reports blocking reasons: "
                        + json.dumps(aggregation["code_reasons"], ensure_ascii=False))
    return problems


def unjudged_cases(run_dir: Path, case: str) -> list:
    """Cases with no contract at all, which a fail-fast finalizer will now judge."""
    root = result_axis_root(run_dir)
    present = set(contract_snapshot_layout(run_dir))
    selected = read_json(run_dir / "formal_aggregation.json").get("selected_cases") or []
    return [c for c in selected if c != case and c not in present]


def reattach_repairs(run_dir: Path) -> int:
    """Re-attach repair accounting the tree finalizer dropped when it rewrote."""
    repairs = repairs_on_disk_layout(run_dir)
    aggregation_path = run_dir / "formal_aggregation.json"
    summary_path = run_dir / "one_stop_summary.json"
    print(f"== re-attaching repair accounting\n   run {run_dir}")
    if not repairs:
        print("   no per-case repair records on disk; nothing to attach")
        return 0
    aggregation = read_json(aggregation_path)
    before = [r.get("case_id") for r in (aggregation.get("result_judging_repairs") or [])]
    aggregation["result_judging_repairs"] = repairs
    write_json(aggregation_path, aggregation)
    if summary_path.is_file():
        summary = read_json(summary_path)
        summary["result_judging_repairs"] = repairs
        write_json(summary_path, summary)
    for block in repairs:
        usage = block.get("usage_total") or {}
        valid = block.get("case_contract_valid_now")
        print(f"   {block['case_id']}: contract_valid_now="
              f"{block.get('repair_succeeded') if valid is None else valid} "
              f"score={block.get('case_result_score_now')} "
              f"logical_requests={usage.get('logical_requests')} "
              f"total_tokens={usage.get('total_tokens')}")
    print(f"   aggregation repairs: {before or 'none'} -> "
          f"{[r['case_id'] for r in repairs]}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", help="required unless --reattach-only")
    parser.add_argument("--reattach-only", action="store_true",
                        help="rewrite only the repair accounting in formal_aggregation.json and "
                             "one_stop_summary.json from the per-case records on disk; starts no "
                             "broker, judges nothing, moves nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify and rehearse only; never archives, never asks the provider")
    parser.add_argument("--no-rehearsal", action="store_true",
                        help="skip the broker-backed finalizer rehearsal (offline checks only); "
                             "only meaningful with --dry-run")
    parser.add_argument("--allow-transport-failure", action="store_true",
                        help="explicitly allow repairing an attempt that was NOT a completed "
                             "HTTP 200 provider response")
    parser.add_argument("--allow-regenerated-inputs", action="store_true",
                        help="allow a repair judging whose case-dir inputs (score caps, oracle "
                             "copy) the finalizer rebuilds and may change")
    parser.add_argument("--allow-additional-judgings", action="store_true",
                        help="on a fail-fast finalizer, allow the re-run to also judge every "
                             "later case that never got a contract")
    parser.add_argument("--task-root", type=Path, help="override the sibling task tree")
    parser.add_argument("--credential-file", type=Path, help="override the evaluator credential file")
    parser.add_argument("--launch-record", type=Path)
    parser.add_argument("--broker-launch", type=Path,
                        help="explicit judge-broker launch.json when a run owns several")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if args.reattach_only:
        return reattach_repairs(run_dir)
    if not args.case:
        raise Refusal("--case is required")
    case = args.case
    print(f"== Result-judge case repair\n   run  {run_dir}\n   case {case}\n   mode "
          + ("DRY-RUN" if args.dry_run else "REPAIR"))

    state = check_preconditions(run_dir, case, args.allow_transport_failure,
                                args.allow_regenerated_inputs)
    print("\n-- preconditions")
    print("   contract_valid ......... False (as required)")
    print("   validation errors ...... OUTPUT-FORMAT only:")
    for item in state["errors"]:
        print("     * " + item)
    row = (state["attempts"].get("attempts") or [{}])[0]
    print(f"   provider attempt ....... http {row.get('http_status')} / "
          f"{row.get('response_status')} / usage_known={row.get('usage_known')} / "
          f"id={row.get('response_id')}")
    print(f"   identity source ........ {state['identity_kind']}"
          + (f" ({state['identity_sha256']} reproduces)" if state['identity_sha256'] else ""))
    print("   immutable inputs ....... all byte-identical on disk")
    if state["regenerated_inputs"]:
        print("   rebuilt in the case dir: "
              + ", ".join(f"{n}[{'reproducible' if n in state['reproducible_inputs'] else 'UNPROVEN'}]"
                          for n in state["regenerated_inputs"]))

    launch = find_launch_record(run_dir, args.launch_record)
    command = launch["command"]
    if args.task_root:
        task_root = args.task_root.resolve()
    else:
        entry = next((token for token in command if token.endswith("formal_one_stop.py")), None)
        if entry is None:
            raise Refusal("launch record does not name the sibling one-stop entry point")
        task_root = Path(entry).resolve().parents[1]
    credential = (args.credential_file or Path(command[command.index("--credential-file") + 1])).resolve()
    # The shared judge is the protocol. scoring_intent binds its digest itself;
    # a standalone tree leaves only the launcher's record of it, so use that.
    launched_judge = launch.get("result_judge_sha256")
    if launched_judge and launched_judge != sha256_file(RESULT_JUDGE):
        raise Refusal("the shared result_judge.py changed since this run was launched "
                      f"({launched_judge[:16]} -> {sha256_file(RESULT_JUDGE)[:16]}); a repair "
                      "judging would not be the protocol the run was measured under")
    config = broker_config(run_dir, args.broker_launch)
    if str(credential) != config["credential"]:
        raise Refusal("launch-record credential and broker credential mount disagree: "
                      f"{credential} vs {config['credential']}")
    print("\n-- recovered run configuration")
    print(f"   task tree .............. {task_root}")
    print(f"   finalizer .............. {task_root / 'evaluator' / 'formal_finalize.py'}")
    print(f"   broker image ........... {config['image']}")
    print(f"   broker upstream ........ {config['upstream']}")
    print(f"   credential ............. {credential} (values never read by this tool)")

    task_token = run_dir.parent.name or "edit"  # .../<family>/<task>/<run>
    family = finalizer_family(task_root)
    supports_output = finalizer_supports_output(task_root, family)
    fail_fast = fail_fast_finalizer(task_root, family)
    if single_shot_finalizer(task_root):
        others = [c for c in contract_snapshot_layout(run_dir) if c != case]
        if others:
            raise Refusal(
                "this tree's finalizer creates every case's judge directory with\n"
                "  mkdir(exist_ok=False) and judges every scoreable case unconditionally, so a\n"
                "  second invocation dies on the first case that already has one ("
                + ", ".join(sorted(others)[:1]) + ").\n"
                "  Archiving this case and re-running would destroy its attempt and still not\n"
                "  judge it.  The tree needs a reuse guard (judge_case must return an existing\n"
                "  valid contract instead of re-judging) before this run can be repaired.")
    extra = unjudged_cases(run_dir, case) if fail_fast else []
    print(f"   finalizer family ....... {family}"
          + ("  (fail-fast: aborts the run on the first unpublishable case)" if fail_fast else ""))
    print(f"   supports --output ...... {supports_output}")
    if extra:
        print(f"   ADDITIONAL JUDGINGS .... re-running also judges {len(extra)} case(s) that never "
              f"got a contract: {', '.join(extra)}")
        if not args.allow_additional_judgings and not args.dry_run:
            raise Refusal(
                f"this tree's finalizer aborts on the first unpublishable case, so {case} is the\n"
                f"  only case that was ever judged.  Repairing it makes the finalizer continue and\n"
                f"  judge {len(extra)} further case(s) ({', '.join(extra)}), each one logical\n"
                "  request.  Pass --allow-additional-judgings to accept that cost.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repair_dir = run_dir / "rejudge" / f"{case}-{stamp}"
    repair_dir.mkdir(parents=True, exist_ok=False)
    log: dict = {"schema_version": "agentswe-edit-result-rejudge-log/v1", "case_id": case,
                 "run_dir": str(run_dir), "tool": str(TOOL_PATH), "tool_sha256": sha256_file(TOOL_PATH),
                 "started_at": utc_now(), "dry_run": bool(args.dry_run),
                 "operator_note": OPERATOR_NOTE, "original_errors": state["errors"],
                 "scoring_identity_sha256": state["identity_sha256"],
                 "task_root": str(task_root), "broker_config": config, "steps": []}

    if args.dry_run and args.no_rehearsal:
        log["steps"].append({"step": "offline_checks_only", "at": utc_now()})
        write_json(repair_dir / "repair_log.json", log)
        print("\n-- plan (offline dry run; nothing started, nothing moved)")
        print(f"   1. archive  {state['case_dir']}\n            -> {state['archive_dir']}")
        print(f"   2. start broker {task_token}-rejudge-judge-* "
              "(fresh cidfile under the run's rejudge/ directory)")
        print("   3. " + " ".join(finalizer_command(task_root, run_dir, credential,
                                                    "http://127.0.0.1:<port>/v1/responses", None,
                                                    finalizer_family(task_root))))
        print("   4. stop broker, annotate formal_aggregation.json / one_stop_summary.json")
        print(f"\n   log: {repair_dir / 'repair_log.json'}")
        return 0

    sys.path.insert(0, str(SHARED_ROOT))
    from judge_broker_runtime import JudgeBroker, stats  # evaluator-owned, unmodified

    port = free_port()
    broker = JudgeBroker(
        name=f"{task_token}-rejudge-judge-"
             + hashlib.sha256(f"{run_dir}:{case}:{time.time_ns()}".encode()).hexdigest()[:12],
        credential=Path(config["credential"]), image=config["image"], port=port,
        cidfile=repair_dir / "result_judge_broker.cid", upstream=config["upstream"])
    print(f"\n-- starting evaluator-owned Result-judge broker on port {port}")
    broker.start()
    endpoint = broker.endpoint
    log["broker"] = {"endpoint": endpoint, "container_id": broker.container_id,
                     "container_name": broker.name, "instance_id": broker.instance_id,
                     "lifecycle": str(broker.lifecycle_path)}
    print(f"   started  {broker.name} ({broker.container_id[:12]}) -> {endpoint}")
    exit_code = 0
    try:
        attestation = run_dir / "formal_scoring" / "result_axis" / "judge_broker_attestation.json"
        if attestation.is_file():
            shutil.copy2(attestation, repair_dir / "judge_broker_attestation.pre-rejudge.json")

        before = stats(endpoint)
        skip = None
        if not supports_output:
            skip = "this tree's finalizer has no --output, so a rehearsal would overwrite the run"
        elif state["regenerated_inputs"]:
            skip = ("a rehearsal would make the finalizer rebuild this case's in-directory inputs ("
                    + ", ".join(state["regenerated_inputs"]) + ") before the attempt is archived, "
                    "destroying the record of what attempt 1 was judged with")
        if skip:
            log["steps"].append({"step": "rehearsal_skipped", "at": utc_now(), "why": skip})
            print("\n-- rehearsal SKIPPED: " + skip + ".")
            print("   The broker started and passed the pinned-protocol check, and every offline")
            print("   precondition holds; there is no call-free way to exercise this tree's")
            print("   finalizer, so the repair pass itself is the first full exercise.")
            if args.dry_run:
                print("\n-- dry run complete; nothing was archived and no judging was issued")
                return 0
            rehearsal, after = None, before
        else:
            rehearsal = "run"
        if rehearsal is not None:
            rehearsal_output = repair_dir / "rehearsal_aggregation.json"
            print("\n-- rehearsal: re-running the tree finalizer to a scratch aggregation "
                  "(no provider call is possible: the prior attempt is still bound)")
            step = run_finalizer(finalizer_command(task_root, run_dir, credential, endpoint,
                                                   rehearsal_output, family), "rehearsal", repair_dir)
            log["steps"].append(step)
            if not rehearsal_output.is_file():
                raise Refusal("rehearsal produced no aggregation; see " + str(repair_dir))
            rehearsal = read_json(rehearsal_output)
            after = stats(endpoint)
            calls = broker_counter(after, "calls") - broker_counter(before, "calls")
            log["steps"][-1]["broker_call_delta"] = calls
            if calls != 0:
                raise Refusal(f"rehearsal unexpectedly issued {calls} provider call(s); aborting")
            problems = check_rehearsal(rehearsal, case, family)
            log["rehearsal_problems"] = problems
            print(f"   finalizer exit {step['returncode']}, provider calls {calls}")
            for reason in (rehearsal.get("result_reasons") or rehearsal.get("reasons") or []):
                print("     reason: " + reason)
            if problems:
                for problem in problems:
                    print("   BLOCKED: " + problem)
                raise Refusal("the finalizer cannot be re-run cleanly for this run; refusing to repair")
            print("   rehearsal clean: the only blocking case is " + case)

        if args.dry_run:
            log["steps"].append({"step": "dry_run_complete", "at": utc_now()})
            print("\n-- dry run complete; nothing was archived and no judging was issued")
            return 0

        # ---- step 2: archive the invalid attempt -------------------------
        case_dir, archive_dir = state["case_dir"], state["archive_dir"]
        evidence_dir = state["evidence_dir"]
        # flat: evidence_dir IS case_dir, so this is the original rename.
        os.rename(evidence_dir, archive_dir)
        archived_inputs = None
        if state["layout"] == "judge_subdir":
            # The bound inputs sit beside judge/, so they survive the rename -- but
            # this tree's finalizer rebuilds them for the case it re-judges.  Copy
            # attempt 1's bytes into the archive so what it was judged with stays
            # on disk next to the attempt itself.
            source = case_dir / "inputs"
            if source.is_dir():
                archived_inputs = archive_dir / "inputs.attempt-001"
                shutil.copytree(source, archived_inputs)
        print(f"\n-- archived {evidence_dir.name} -> {archive_dir.name} "
              "(moved, bytes preserved)")
        if archived_inputs is not None:
            print(f"   attempt-1 staged inputs copied to {archived_inputs}")
        log["steps"].append({"step": "archive", "at": utc_now(), "from": str(evidence_dir),
                             "to": str(archive_dir), "layout": state["layout"],
                             "archived_inputs": str(archived_inputs) if archived_inputs else None})
        original_usage = state["contract"].get("provider_usage") or {}
        record = {
            "schema_version": "agentswe-edit-result-rejudge-record/v1",
            "case_id": case, "reason": "shared Result judge output failed the judge's own "
                                       "contract validation for output-format reasons only",
            "operator_note": OPERATOR_NOTE,
            "original_validation_errors": state["errors"],
            "original_attempt_archive": str(archive_dir),
            "per_case_evidence_layout": state["layout"],
            "original_attempt_inputs_archive": str(archived_inputs) if archived_inputs else None,
            "original_provider_response_id": row.get("response_id"),
            "original_provider_http_status": row.get("http_status"),
            "original_provider_response_status": row.get("response_status"),
            "original_provider_usage": original_usage,
            "original_contract_created_at": state["contract"].get("created_at"),
            "scoring_identity_sha256": state["identity_sha256"],
            "transport_failure_override": bool(args.allow_transport_failure),
            "identity_source": state["identity_kind"],
            "finalizer_family": family,
            "inputs_rebuilt_by_finalizer": state["regenerated_inputs"],
            "inputs_rebuilt_unproven": state["unproven_inputs"],
            "regenerated_inputs_override": bool(args.allow_regenerated_inputs),
            "additional_cases_judged_by_this_repair": extra,
            "repair_judging_started_at": utc_now(),
            "repair_broker": {"endpoint": endpoint, "container_name": broker.name,
                              "instance_id": broker.instance_id, "image": config["image"]},
            "tool": str(TOOL_PATH),
            # the sibling tree can be patched between a run and its repair; record
            # what actually judged this attempt so the drift stays auditable
            "finalizer": str(finalizer_path(task_root)),
            "finalizer_sha256": sha256_file(finalizer_path(task_root)),
            "shared_result_judge_sha256": sha256_file(RESULT_JUDGE),
        }
        # flat: the case dir was just moved away and is recreated for the record.
        # judge_subdir: only judge/ moved; the case dir (and inputs/) still stands,
        # and result_judge.py makes its own --output-dir (result_judge.py:1077).
        case_dir.mkdir(parents=True, exist_ok=state["layout"] != "flat")
        write_json(case_dir / "rejudge_record.json", record)

        # ---- steps 3+4: one new logical request + real aggregation -------
        aggregation_path = run_dir / "formal_aggregation.json"
        shutil.copy2(aggregation_path, repair_dir / "formal_aggregation.pre-rejudge.json")
        summary_path = run_dir / "one_stop_summary.json"
        if summary_path.is_file():
            shutil.copy2(summary_path, repair_dir / "one_stop_summary.pre-rejudge.json")
        print("\n-- repair judging: one NEW logical request through the fresh broker, "
              "byte-identical inputs")
        step = run_finalizer(finalizer_command(task_root, run_dir, credential, endpoint, None, family),
                             "repair", repair_dir)
        log["steps"].append(step)
        final_stats = stats(endpoint)
        repair_calls = broker_counter(final_stats, "calls") - broker_counter(after, "calls")
        log["steps"][-1]["broker_call_delta"] = repair_calls
        print(f"   finalizer exit {step['returncode']}, provider calls {repair_calls}")

        new_contract_path = evidence_dir / CONTRACT_NAME
        new_contract = read_json(new_contract_path) if new_contract_path.is_file() else {}
        new_usage = new_contract.get("provider_usage") or {}
        new_attempts_path = evidence_dir / "provider_response-attempts.json"
        new_rows = (read_json(new_attempts_path).get("attempts")
                    if new_attempts_path.is_file() else []) or [{}]
        aggregation = read_json(aggregation_path)
        repaired = (new_contract.get("contract_valid") is True
                    and aggregation.get("formal_result_publishable") is True)

        repair_block = {
            "schema_version": "agentswe-edit-result-rejudge/v1",
            "case_id": case, "reason": "schema-invalid judge output over a completed provider "
                                       "response; one repair judging",
            "operator_note": OPERATOR_NOTE, "repaired_at": utc_now(), "tool": str(TOOL_PATH),
            "repair_succeeded": new_contract.get("contract_valid") is True,
            "run_publishable_after_repair": bool(aggregation.get("formal_result_publishable")),
            "scoring_identity_sha256": state["identity_sha256"],
            "inputs_byte_identical": not state["unproven_inputs"],
            "inputs_rebuilt_by_finalizer": state["regenerated_inputs"],
            "inputs_rebuilt_unproven": state["unproven_inputs"],
            "additional_cases_judged": extra,
            "attempts": [
                {"attempt": 1, "outcome": "model_output_invalid",
                 "validation_errors": state["errors"],
                 "archived_case_dir": str(archive_dir),
                 "archived_inputs_dir": str(archived_inputs) if archived_inputs else None,
                 "provider_response_id": row.get("response_id"),
                 "provider_usage": original_usage},
                {"attempt": 2, "outcome": new_contract.get("evaluation_state"),
                 "contract_valid": new_contract.get("contract_valid"),
                 "result_score": new_contract.get("result_score"),
                 "case_dir": str(case_dir), "evidence_dir": str(evidence_dir),
                 "provider_response_id": new_rows[0].get("response_id"),
                 "provider_usage": new_usage},
            ],
            "usage_total": {
                "logical_requests": int(original_usage.get("logical_requests") or 0)
                                    + int(new_usage.get("logical_requests") or 0),
                "input_tokens": int(original_usage.get("input_tokens") or 0)
                                + int(new_usage.get("input_tokens") or 0),
                "output_tokens": int(original_usage.get("output_tokens") or 0)
                                 + int(new_usage.get("output_tokens") or 0),
                "total_tokens": int(original_usage.get("total_tokens") or 0)
                                + int(new_usage.get("total_tokens") or 0)},
            "note": "result_judge_contracts and the broker attestation account for attempt 2 only; "
                    "attempt 1 usage is accounted here and in the archived attempt directory.",
        }
        write_json(case_dir / "rejudge_record.json", {**record, "repair": repair_block})
        # the finalizer just rewrote the aggregation from scratch, so every
        # earlier repair's usage accounting has to be re-attached, not replaced
        repairs = repairs_on_disk_layout(run_dir)
        aggregation["result_judging_repairs"] = repairs
        write_json(aggregation_path, aggregation)

        if summary_path.is_file():
            summary = read_json(summary_path)
            summary.update({
                "result_axis": aggregation.get("result_axis"),
                "formal_result_claimed": bool(aggregation.get("formal_result_publishable")),
                "code_score_claimed": bool(aggregation.get("code_score_publishable")),
                "formal_finalizer_exit": step["returncode"],
                "result_judging_repairs": repairs,
                "result_judge_broker_owned_by_one_stop": False,
                "post_run_repair": {
                    "note": "these fields were refreshed from formal_aggregation.json by the "
                            "per-case Result-judge repair tool, not by formal_one_stop",
                    "tool": str(TOOL_PATH), "at": utc_now(),
                    "pre_repair_copy": str(repair_dir / "one_stop_summary.pre-rejudge.json")},
            })
            write_json(summary_path, summary)

        exit_code = 0 if repaired else 3
        print("\n== summary")
        print(f"   attempt 1 : INVALID  ({len(state['errors'])} format errors) -> {archive_dir}")
        print(f"   attempt 2 : {new_contract.get('evaluation_state')}  "
              f"contract_valid={new_contract.get('contract_valid')}  "
              f"result_score={new_contract.get('result_score')}")
        print(f"   usage     : {repair_block['usage_total']}")
        print(f"   aggregation: publishable={aggregation.get('formal_result_publishable')}  "
              f"result_axis={json.dumps(aggregation.get('result_axis'), ensure_ascii=False)}")
        for reason in aggregation.get("reasons") or []:
            print("   reason: " + reason)
        print(f"   evidence  : {repair_dir}")
        return exit_code
    finally:
        log["finished_at"] = utc_now()
        try:
            log["broker_cleanup"] = broker.close()
            print(f"\n-- stopped broker {broker.name}: "
                  f"absent_after_cleanup={log['broker_cleanup'].get('absent_after_cleanup')}")
        except Exception as exc:  # noqa: BLE001
            log["broker_cleanup_error"] = f"{type(exc).__name__}: {exc}"
            print(f"\n!! broker cleanup error: {type(exc).__name__}: {exc}")
        write_json(repair_dir / "repair_log.json", log)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        raise SystemExit(4)
