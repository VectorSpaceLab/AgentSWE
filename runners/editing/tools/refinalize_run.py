#!/usr/bin/env python3
"""Re-run a completed formal run's tree finalizer under a fresh judge broker.

Companion to rejudge_case.py.  Where that tool repairs ONE case whose judging
produced a schema-invalid answer, this tool changes no case at all: it starts
the run's own evaluator-owned Result-judge broker, re-runs the sibling tree's
finalizer exactly as formal_one_stop does, and stops the broker again.

Nothing is archived and no existing contract is touched.  execution_scoring's
scoring_intent gate means every case that already has a bound judging is reused
from disk; only cases that never had one can produce a NEW logical request, and
each such case still makes exactly one.  The tool verifies both properties
afterwards: pre-existing contracts must come back byte-identical, and the
broker's call delta must equal the transport attempts of the newly judged cases.

Intended use: a run whose finalizer refused for a deterministic, evidence-level
reason (a missing binding field, a routing defect) that has since been fixed in
the sibling tree, where nothing has to be re-executed.

Nothing under @@AGENTSWE_LEGACY_HOME@@ is written.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never leave .pyc files in the registry-pinned shared tree

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edit_run_layouts import (  # noqa: E402
    CASES, CONTROL_PYTHON, SHARED_ROOT, Refusal, broker_config, broker_counter, finalizer_path,
    contract_snapshot, credential_from_launch, fail_fast_finalizer, finalizer_command,
    finalizer_family, finalizer_supports_output, find_launch_record, free_port, read_json,
    endpoint_flag, finalizer_resamples_refused, repairs_on_disk, result_axis_root, run_finalizer,
    sha256_file, task_root_from_launch,
    utc_now, write_json)

TOOL_PATH = Path(__file__).resolve()


# ---------------------------------------------------------------------------
# what the finalizer would do
# ---------------------------------------------------------------------------
def hidden_record_digests(run_dir: Path) -> dict:
    """sha256 of each task-native hidden case record, wherever the tree keeps it."""
    digests = {}
    for case in CASES:
        found = None
        for candidate in (run_dir / "lifecycle" / "hidden" / case / "case_result.json",
                          run_dir / "hidden" / "cases" / case / "case_result.json",
                          run_dir / "hidden" / case / "case_result.json"):
            if candidate.is_file():
                found = candidate
                break
        digests[case] = {"path": str(found), "sha256": sha256_file(found)} if found else None
    return digests


PROBE = """import json, runpy, sys, traceback
sys.dont_write_bytecode = True
from pathlib import Path

EVALUATOR = '@EVALUATOR@'
FINALIZE = '@FINALIZE@'
MODNAME = '@MODNAME@'
OUT = Path('@OUT@')
RUN = Path('@RUN@')
SCRATCH = Path('@SCRATCH@')
sys.path.insert(0, EVALUATOR)
import case_evidence

notes = {'prepared': {}, 'judge_calls': [], 'patched': False, 'errors': []}
_original = case_evidence.prepare_case_evidence


def patch_module():
    # The shim registers its formal_axes_shared instance under a private name
    # before running it, so it can be reached from here.  Two substitutions make
    # the publisher's own per-case decisions observable without any provider
    # call and without writing a single judging artefact:
    #   judge_broker_stats -> a canned zero reading, so result_broker_ready is
    #     True and the per-case gates AFTER it are actually evaluated;
    #   judge_execution_case -> a stub, so a case that reaches judging is
    #     recorded instead of judged.
    module = sys.modules.get(MODNAME)
    if module is None or notes['patched']:
        return
    notes['patched'] = True
    real_write = module.write_json

    def guarded_write(path, value):
        # a prediction observes; it never writes into the run -- but its own
        # evidence directory lives inside the run, so that one is exempt
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(SCRATCH)
            return real_write(path, value)
        except ValueError:
            pass
        try:
            resolved.relative_to(RUN)
        except ValueError:
            return real_write(path, value)
        notes.setdefault('suppressed_writes', []).append(str(path))
    module.write_json = guarded_write
    module.judge_broker_stats = lambda endpoint: {
        'schema_version': 'agentswe-judge-broker-stats/v1',
        'runtime': {'calls': 0, 'completed_calls': 0, 'successful_calls': 0, 'failures': 0,
                    'upstream_attempts': 0, 'tokens': 0, 'usage_unknown_calls': 0,
                    'in_flight_calls': 0}}

    def judge_stub(**kw):
        notes['judge_calls'].append(kw.get('case_id'))
        return {'classification': 'unresolved', 'score': None, 'contract_valid': False,
                'round_consumed': False, 'contract_path': None, 'usage': None,
                'reason': 'prediction probe: no judging performed', 'cached': False,
                'assessment': None, 'major_errors': None, 'dimensions': {}}
    module.judge_execution_case = judge_stub


def traced(**kw):
    patch_module()
    case = kw.get('case_id')
    try:
        out = _original(**kw)
    except BaseException as exc:
        notes['prepared'][case] = {'error': type(exc).__name__ + ': ' + str(exc),
                                   'traceback': traceback.format_exc()[-1500:]}
        raise
    record = out['execution_record']
    notes['prepared'][case] = {'case_id': record.get('case_id'),
                               'candidate_digest': record.get('candidate_digest'),
                               'classification': record.get('classification')}
    return out


case_evidence.prepare_case_evidence = traced
sys.argv = [FINALIZE] + sys.argv[1:]
try:
    runpy.run_path(FINALIZE, run_name='__main__')
except SystemExit as exc:
    notes['exit_code'] = exc.code
except BaseException as exc:
    notes['driver_error'] = type(exc).__name__ + ': ' + str(exc)
OUT.write_text(json.dumps(notes, indent=2, sort_keys=True), encoding='utf-8')
"""


def probe_tree_records(run_dir: Path, task_root: Path, credential: Path, scratch: Path) -> dict:
    """Simulate the real pass through the tree's own finalizer, spending nothing.

    Running the tree's adapter alone is not enough: it answers what the adapter
    BUILDS, not what the publisher RECEIVES, and those differ whenever the
    sibling rebinds a shared function the publisher also recurses through.  The
    probe therefore runs the real entry point with the broker reading and the
    judging call substituted, so the aggregation it produces is the one a real
    pass would produce and `judge_calls` is the true judgeable set.
    """
    script = scratch / "probe_records.py"
    out = scratch / "probe_records.json"
    aggregation = scratch / "probe_aggregation.json"
    script.write_text(PROBE.replace("@EVALUATOR@", str(task_root / "evaluator"))
                      .replace("@FINALIZE@", str(finalizer_path(task_root)))
                      .replace("@MODNAME@", shim_module_name(task_root))
                      .replace("@RUN@", str(run_dir))
                      .replace("@SCRATCH@", str(scratch))
                      .replace("@OUT@", str(out)), encoding="utf-8")
    command = [str(CONTROL_PYTHON), "-E", "-s", "-B", str(script),
               "--run-dir", str(run_dir), "--credential-file", str(credential),
               endpoint_flag("semantic_finalize"), "http://127.0.0.1:1/v1/responses",
               "--output", str(aggregation)]
    step = run_finalizer(command, "probe", scratch)
    notes = read_json(out) if out.is_file() else {}
    return {"step": step, "notes": notes,
            "aggregation": read_json(aggregation) if aggregation.is_file() else {}}


def shim_module_name(task_root: Path) -> str:
    """The private module name the sibling registers its shared-axes instance under."""
    # Trees do not agree on the shim's filename: 13 openhands uses
    # evaluator/shared_finalize.py, 16 ai-scientist uses evaluator/ai_shared_finalize.py.
    # Reading only the first name made predict_judgings raise FileNotFoundError on every
    # ai-scientist run before any evidence was read.
    evaluator = task_root / "evaluator"
    shim = next((path for path in (evaluator / "shared_finalize.py",
                                   *sorted(evaluator.glob("*shared_finalize.py")))
                 if path.is_file()), None)
    if shim is None:
        return ""
    text = shim.read_text(encoding="utf-8")
    marker = "spec_from_file_location("
    index = text.find(marker)
    if index < 0:
        return ""
    fragment = text[index + len(marker):index + len(marker) + 120]
    quote = fragment[0]
    return fragment[1:fragment.index(quote, 1)] if quote in "\"'" else ""


def pre_judge_refusal(record: dict):
    """The pre-judge gates every tree applies to a frozen execution record.

    `shared.infrastructure_reason` reads the classification tokens; the task
    finalizers additionally refuse an explicitly infrastructure-flagged record and
    one whose lower broker recorded a failure (aider's gate, formal_finalize.py:
    408-410: ``classification in INFRA or infrastructure_invalid is True or
    int(broker.failures_delta or 0) != 0``).  Both are cheap to read here and are
    what separates "not judged yet" from "would be judged".
    """
    if record.get("infrastructure_invalid") is True:
        return "record is flagged infrastructure_invalid"
    broker = record.get("broker") if isinstance(record.get("broker"), dict) else {}
    try:
        failures = int(broker.get("failures_delta", 0) or 0)
    except (TypeError, ValueError):
        failures = 0
    if failures:
        return f"lower broker recorded {failures} failure(s) for this case"
    classification = str(record.get("classification") or "")
    if "infrastructure" in classification:
        return f"classification is {classification}"
    return None


def predict_judgings(run_dir: Path, task_root: Path, family: str, credential: Path,
                     scratch: Path) -> dict:
    """Replicate the finalizer's own gates to see which cases could be judged.

    Uses the shared modules the finalizer uses, and -- on the trees that build
    their own records -- the tree's adapter itself, so the prediction cannot
    drift from the implementation.  It is deliberately conservative: gates the
    finalizer applies after this point can only remove cases, never add them.
    """
    root = result_axis_root(run_dir)
    if family == "standalone":
        # A case with no contract has not been judged YET; that is not the same as
        # "the finalizer would judge it".  Every tree refuses an infrastructure-
        # attributed case before it ever reaches the judge, so the execution record
        # -- not the absence of a contract -- decides.
        sys.path.insert(0, str(SHARED_ROOT))
        import formal_axes_shared as shared
        hidden_path, hidden = shared.hidden_document(run_dir)
        present = set(contract_snapshot(run_dir))
        fail_fast = fail_fast_finalizer(task_root, family)
        resamples = finalizer_resamples_refused(task_root)
        blocked = None
        rows = {}
        for case in CASES:
            case_dir = root / case
            contract_path = case_dir / "result_score_contract.json"
            if case in present:
                contract = read_json(contract_path)
                valid = contract.get("contract_valid") is True
                if valid:
                    state = "already_bound"
                elif resamples:
                    # the tree retires this verdict and judges once on the next pass
                    state = "would_be_resampled"
                else:
                    state = "already_bound_but_invalid"
                rows[case] = {"state": state, "already_bound": True, "contract_valid": valid}
                if state == "already_bound_but_invalid" and fail_fast and blocked is None:
                    blocked = case
                continue
            if blocked is not None:
                # this finalizer aborts the run at `blocked`, so nothing after it
                # is ever reached, however judgeable the case itself is
                rows[case] = {"state": "unreachable_until_" + blocked, "already_bound": False}
                continue
            record = shared.find_case_record(hidden, case) if hidden_path else None
            if record is None:
                rows[case] = {"state": "would_be_judged", "already_bound": False,
                              "record_found": False}
                continue
            refusal = shared.infrastructure_reason(record) or pre_judge_refusal(record)
            if refusal:
                rows[case] = {"state": "refused_by_finalizer", "already_bound": False,
                              "finalizer_reason": refusal, "record_found": True}
                continue
            rows[case] = {"state": "would_be_judged", "already_bound": False, "record_found": True}
        rows["_frozen_digest"] = ""
        rows["_probe"] = None
        return rows

    sys.path.insert(0, str(SHARED_ROOT))
    import formal_axes_shared as shared
    from execution_contract import classify_candidate_execution

    probe = None
    prepared = {}
    judgeable = None
    if family == "semantic_finalize":
        probe = probe_tree_records(run_dir, task_root, credential, scratch)
        prepared = (probe["notes"] or {}).get("prepared") or {}
        if (probe["notes"] or {}).get("patched"):
            judgeable = list((probe["notes"] or {}).get("judge_calls") or [])
        probe_reasons = (probe["aggregation"] or {}).get("result_reasons") or []

    hidden_path, hidden = shared.hidden_document(run_dir)
    freeze_path, freeze = shared.freeze_document(run_dir)
    if hidden_path is None or freeze_path is None:
        raise Refusal("run lacks complete hidden-after-freeze or frozen-Candidate evidence "
                      f"that the shared publisher can read (looked under {run_dir})")
    frozen = str(freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest") or "")
    rows = {}
    for case in CASES:
        case_dir = root / case
        bound = ((case_dir / "scoring_intent.json").exists()
                 or (case_dir / "result_score_contract.json").exists())
        if family == "semantic_finalize":
            built = prepared.get(case) or {}
            if judgeable is not None:
                # the simulation ran the publisher itself; believe it, not a replay
                reason = next((r for r in probe_reasons if r.startswith(case + ": ")), None)
                if case in judgeable:
                    state = "would_be_judged"
                elif case in ((probe["aggregation"] or {}).get("candidate_zero_cases") or []):
                    state = "candidate_zero"
                elif bound:
                    state = "already_bound"
                else:
                    state = "refused_by_finalizer"
                rows[case] = {"state": state, "already_bound": bound,
                              "finalizer_reason": reason.split(": ", 1)[1] if reason else None,
                              "adapter_built_candidate_digest": built.get("candidate_digest"),
                              "evidence_preparation_error": built.get("error")}
                continue
            if built.get("error"):
                rows[case] = {"state": "evidence_preparation_failed", "already_bound": bound,
                              "evidence_preparation_error": built["error"]}
                continue
            if not built.get("record"):
                rows[case] = {"state": "not_prepared", "already_bound": bound}
                continue
            record = built["record"]
        else:
            record = shared.find_case_record(hidden, case) or {}
        infra = shared.infrastructure_reason(record)
        verdict = classify_candidate_execution(record, case_id=case, candidate_digest=frozen)
        calls, successful = shared.lower_execution_counts(record)
        if infra:
            state = "infrastructure_invalid"
        elif verdict["classification"] == "candidate_zero":
            state = "candidate_zero"
        elif record.get("case_id") != case or record.get("candidate_digest") != frozen:
            state = "not_bound_to_frozen_digest"
        elif verdict["classification"] != "scoreable":
            state = verdict["classification"]
        elif family != "semantic_finalize" and (calls <= 0 or successful <= 0):
            state = "no_successful_lower_call"
        elif bound:
            state = "already_bound"
        else:
            state = "would_be_judged"
        rows[case] = {"state": state, "already_bound": bound,
                      "classification": verdict["classification"]}
    rows["_frozen_digest"] = frozen
    rows["_probe"] = (probe or {}).get("step")
    return rows


SHIM_DRIVER = """import hashlib, json, runpy, sys, traceback
sys.dont_write_bytecode = True
from pathlib import Path

EVALUATOR = '@EVALUATOR@'
FINALIZE = '@FINALIZE@'
RUN = Path('@RUN@')
SCRATCH = Path('@SCRATCH@')
OUT = Path('@OUT@')
CASES = ['test_001', 'test_002', 'test_003', 'test_004', 'test_005', 'test_006']
sys.path.insert(0, EVALUATOR)


def sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        return 'ERR:' + type(exc).__name__


import case_evidence
from case_evidence import tree_digest


def snapshot():
    frozen = RUN / 'lifecycle' / 'frozen_candidate'
    try:
        digest = tree_digest(frozen) if frozen.is_dir() else None
    except BaseException as exc:
        digest = 'ERR:' + type(exc).__name__ + ': ' + str(exc)
    records, bundles = {}, {}
    for case in CASES:
        path = RUN / 'lifecycle' / 'hidden' / case / 'case_result.json'
        records[case] = sha(path) if path.is_file() else None
        bundle = RUN / 'formal_scoring' / 'result_axis' / case / 'inputs'
        bundles[case] = ({f.name: sha(f) for f in sorted(bundle.glob('*.json'))}
                         if bundle.is_dir() else None)
    return {'frozen_candidate_tree_digest': digest, 'case_result_sha256': records,
            'inputs_bundle_sha256': bundles, 'observed_at': __import__('time').time()}


seen = {}
_original = case_evidence.prepare_case_evidence


def traced(**kw):
    case = kw.get('case_id')
    entry = {'record_path': str(kw.get('record_path')), 'candidate': str(kw.get('candidate')),
             'candidate_digest_argument': kw.get('candidate_digest'), 'output': str(kw.get('output'))}
    try:
        out = _original(**kw)
    except BaseException as exc:
        entry['error'] = type(exc).__name__ + ': ' + str(exc)
        entry['traceback'] = traceback.format_exc()
        seen[case] = entry
        raise
    record = out['execution_record']
    entry.update(case_id=record.get('case_id'), classification=record.get('classification'),
                 record_candidate_digest=record.get('candidate_digest'))
    seen[case] = entry
    return out


case_evidence.prepare_case_evidence = traced

before = snapshot()
sys.argv = [FINALIZE] + sys.argv[1:]
exit_code, driver_error = None, None
try:
    runpy.run_path(FINALIZE, run_name='__main__')
except SystemExit as exc:
    exit_code = exc.code
except BaseException as exc:
    driver_error = type(exc).__name__ + ': ' + str(exc) + chr(10) + traceback.format_exc()
after = snapshot()
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps({
    'cases': seen, 'before': before, 'after': after,
    'exit_code': exit_code, 'driver_error': driver_error,
    'frozen_digest_changed':
        before['frozen_candidate_tree_digest'] != after['frozen_candidate_tree_digest'],
    'case_results_changed': [c for c in before['case_result_sha256']
                             if before['case_result_sha256'][c] != after['case_result_sha256'][c]],
    'bundles_changed': [c for c in before['inputs_bundle_sha256']
                        if before['inputs_bundle_sha256'][c] != after['inputs_bundle_sha256'][c]],
}, indent=2, sort_keys=True), encoding='utf-8')
sys.exit(exit_code if isinstance(exit_code, int) else (1 if driver_error else 0))
"""


def shim_debug_command(task_root: Path, run_dir: Path, credential: Path, endpoint: str,
                       debug_path: Path, scratch: Path, output=None) -> list:
    """Run the REAL finalizer entry point, with the record adapter traced.

    runpy executes evaluator/formal_finalize.py under __main__ with the same
    argv the plain subprocess would get, so the layout/code-rubric prefix, the
    shim and the shared publisher are exactly the ones a normal pass uses.  The
    only addition is that case_evidence.prepare_case_evidence is wrapped before
    the import inside shared_finalize.finalize resolves it, and that the frozen
    tree and the hidden case records are digested immediately before and after.
    """
    script = scratch / "shim_debug_driver.py"
    text = (SHIM_DRIVER.replace("@EVALUATOR@", str(task_root / "evaluator"))
            .replace("@FINALIZE@", str(finalizer_path(task_root)))
            .replace("@RUN@", str(run_dir)).replace("@OUT@", str(debug_path)))
    script.write_text(text, encoding="utf-8")
    command = [str(CONTROL_PYTHON), "-E", "-s", "-B", str(script),
               "--run-dir", str(run_dir), "--credential-file", str(credential),
               endpoint_flag("semantic_finalize"), endpoint]
    if output is not None:
        command.extend(["--output", str(output)])
    return command


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--reason", help="why this run is being re-finalized; required for a real run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-rehearsal", action="store_true",
                        help="offline checks only; never starts a broker")
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--tree", type=Path,
                        help="alias for --task-root: run against an alternate copy of the "
                             "sibling tree (default: the tree the launch record names)")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--launch-record", type=Path)
    parser.add_argument("--broker-launch", type=Path)
    parser.add_argument("--shim-debug", type=Path,
                        help="run the real finalizer through a traced driver and write the "
                             "per-case record-adapter exceptions plus before/after digests here")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if not args.dry_run and not (args.reason or "").strip():
        raise Refusal("--reason is required for a real re-finalization")
    reason = (args.reason or "dry run").strip()

    print(f"== formal re-finalization\n   run  {run_dir}\n   mode "
          + ("DRY-RUN" if args.dry_run else "REFINALIZE"))

    # COMPLETION_MARKERS: the check is "this run has ended", not "this run wrote one
    # particular file".  Families differ: the aider tree never writes cleanup_attestation.json
    # at all, so requiring it refused every finished aider run outright.  A run that already
    # produced formal_aggregation.json has completed its finalizer, and a terminal container
    # cleanup record beside it shows the containers were torn down; that pair is the
    # equivalent evidence.  builder_direct_cleanup.json is excluded on purpose: it is written
    # right after the Builder phase, long before hidden execution ends.
    COMPLETION_MARKERS = ("cleanup_attestation.json", "cleanup_manifest.json",
                          "retained_manifest.json")
    FALLBACK_MARKERS = ("builder_container_cleanup.json",)
    completion_marker = next(
        (name for name in COMPLETION_MARKERS if (run_dir / name).is_file()), None)
    if completion_marker is None and (run_dir / "formal_aggregation.json").is_file():
        completion_marker = next(
            (name for name in FALLBACK_MARKERS if (run_dir / name).is_file()), None)
    if completion_marker is None:
        raise Refusal("run has no completion marker ("
                      + ", ".join(COMPLETION_MARKERS + FALLBACK_MARKERS)
                      + "); it may still be in flight -- refusing")
    print(f"   done via completion marker {completion_marker}")
    aggregation_path = run_dir / "formal_aggregation.json"
    if not aggregation_path.is_file():
        raise Refusal("run has no formal_aggregation.json; this tool only re-finalizes a run that "
                      "already reached its finalizer")
    before_aggregation = read_json(aggregation_path)
    if before_aggregation.get("formal_result_publishable") is True:
        raise Refusal("this run is already publishable; refusing to re-finalize a published result")

    launch = find_launch_record(run_dir, args.launch_record)
    command = launch["command"]
    if args.task_root and args.tree:
        raise Refusal("pass --task-root or --tree, not both")
    task_root = task_root_from_launch(launch, args.task_root or args.tree)
    credential = credential_from_launch(launch, args.credential_file)
    config = broker_config(run_dir, args.broker_launch)
    family = finalizer_family(task_root)
    supports_output = finalizer_supports_output(task_root, family)
    fail_fast = fail_fast_finalizer(task_root, family)
    if str(credential) != config["credential"]:
        raise Refusal("launch-record credential and broker credential mount disagree")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = run_dir / "refinalize" / stamp
    evidence_dir.mkdir(parents=True, exist_ok=False)
    snapshot = contract_snapshot(run_dir)
    hidden_records_before = hidden_record_digests(run_dir)
    prediction = predict_judgings(run_dir, task_root, family, credential, evidence_dir)
    frozen = prediction.pop("_frozen_digest")
    probe_step = prediction.pop("_probe", None)
    would_judge = [case for case, row in prediction.items()
                   if row["state"] in ("would_be_judged", "would_be_resampled")]
    blocking = sorted(case for case, row in prediction.items()
                      if row.get("state") == "already_bound_but_invalid")
    unreachable = sorted(case for case, row in prediction.items()
                         if str(row.get("state", "")).startswith("unreachable_until_"))

    print("\n-- current state")
    print(f"   aggregation ............ publishable={before_aggregation.get('formal_result_publishable')}"
          f"  result_axis={json.dumps(before_aggregation.get('result_axis'), ensure_ascii=False)[:60]}")
    for line in (before_aggregation.get("result_reasons") or [])[:8]:
        print("     reason: " + line)
    print(f"   frozen Candidate digest  {frozen}")
    print(f"   existing contracts ..... {len(snapshot)} ({', '.join(sorted(snapshot)) or 'none'})")
    print(f"   finalizer family ....... {family}"
          + ("  (fail-fast)" if fail_fast else "")
          + (f"  supports --output={supports_output}"))
    print("   per-case finalizer gate:")
    for case in sorted(prediction):
        row = prediction[case]
        detail = row.get("evidence_preparation_error")
        if row.get("state") != "would_be_judged":
            detail = detail or row.get("finalizer_reason")
        print(f"     {case}: {row['state']}" + (f"  <- {detail}" if detail else ""))
    print(f"   NEW logical judge requests this re-finalization would issue: {len(would_judge)}"
          + (f" ({', '.join(would_judge)})" if would_judge else ""))
    if blocking and unreachable:
        print(f"   BLOCKED: {', '.join(blocking)} already hold an INVALID contract and this "
              "finalizer aborts there.")
        print(f"            {len(unreachable)} later case(s) are unreachable until that is "
              "repaired: " + (", ".join(unreachable) or "none"))
        print("            Re-finalizing alone changes nothing; repair the case first with "
              "rejudge_case.py.")
    elif blocking:
        print(f"   BLOCKED: {', '.join(blocking)} hold an INVALID contract and this tree's "
              "finalizer neither reuses")
        print("            nor resamples a bound verdict, so re-finalizing cannot change them.")

    print("\n-- recovered run configuration")
    print(f"   task tree .............. {task_root}")
    print(f"   finalizer .............. {task_root / 'evaluator' / 'formal_finalize.py'}")
    print(f"   broker image ........... {config['image']}")
    print(f"   broker upstream ........ {config['upstream']}")
    print(f"   credential ............. {credential} (values never read by this tool)")

    record: dict = {
        "schema_version": "agentswe-edit-formal-refinalize/v1",
        "run_dir": str(run_dir), "reason": reason, "dry_run": bool(args.dry_run),
        "started_at": utc_now(), "tool": str(TOOL_PATH), "tool_sha256": sha256_file(TOOL_PATH),
        "task_root": str(task_root), "broker_config": config,
        "finalizer_family": family, "finalizer_supports_output": supports_output,
        "task_root_is_override": bool(args.task_root or args.tree),
        "fail_fast_finalizer": fail_fast, "record_probe": probe_step,
        "frozen_candidate_digest": frozen,
        "pre_existing_contracts": snapshot,
        "hidden_case_record_sha256_before": hidden_records_before,
        "finalizer_gate_prediction": prediction,
        "cases_that_would_be_judged": would_judge,
        "cases_blocking_a_fail_fast_finalizer": blocking,
        "cases_unreachable_until_repaired": unreachable,
        "steps": [],
    }
    shutil.copy2(aggregation_path, evidence_dir / "formal_aggregation.pre-refinalize.json")
    summary_path = run_dir / "one_stop_summary.json"
    if summary_path.is_file():
        shutil.copy2(summary_path, evidence_dir / "one_stop_summary.pre-refinalize.json")

    real_command = finalizer_command(task_root, run_dir, credential,
                                     "http://127.0.0.1:<port>/v1/responses", None, family)
    record["finalizer_argv_template"] = real_command

    if blocking and not would_judge and not args.no_rehearsal:
        record["steps"].append({"step": "nothing_to_do", "at": utc_now()})
        write_json(evidence_dir / "refinalize_record.json", record)
        raise Refusal(
            "re-finalizing this run cannot change anything: " + ", ".join(blocking)
            + " already hold an invalid contract\n  and this tree's finalizer aborts there, so no "
              "later case is ever reached.  Repair the\n  blocking case with rejudge_case.py first.")

    if args.no_rehearsal:
        record["steps"].append({"step": "offline_checks_only", "at": utc_now()})
        write_json(evidence_dir / "refinalize_record.json", record)
        print("\n-- offline dry run; nothing started")
        print("   finalizer argv: " + " ".join(real_command))
        print(f"   evidence: {evidence_dir}")
        return 0

    sys.path.insert(0, str(SHARED_ROOT))
    from judge_broker_runtime import JudgeBroker, stats  # evaluator-owned, unmodified

    task = run_dir.parent.name or "edit"  # .../<family>/<task>/<run>
    port = free_port()
    broker = JudgeBroker(
        name=f"{task}-refinalize-judge-"
             + hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()[:12],
        credential=Path(config["credential"]), image=config["image"], port=port,
        cidfile=evidence_dir / "result_judge_broker.cid", upstream=config["upstream"])
    print(f"\n-- starting evaluator-owned Result-judge broker on port {port}")
    broker.start()
    endpoint = broker.endpoint
    record["broker"] = {"endpoint": endpoint, "container_id": broker.container_id,
                        "container_name": broker.name, "instance_id": broker.instance_id,
                        "lifecycle": str(broker.lifecycle_path)}
    print(f"   started  {broker.name} ({broker.container_id[:12]}) -> {endpoint}")
    try:
        attestation = run_dir / "formal_scoring" / "result_axis" / "judge_broker_attestation.json"
        if attestation.is_file():
            shutil.copy2(attestation, evidence_dir / "judge_broker_attestation.pre-refinalize.json")
        before_stats = stats(endpoint)

        if args.dry_run:
            if would_judge:
                record["steps"].append({"step": "rehearsal_skipped", "at": utc_now(),
                                        "why": "a rehearsal would issue real judgings"})
                print("\n-- rehearsal SKIPPED: this run has "
                      f"{len(would_judge)} case(s) with no bound judging, so any finalizer")
                print("   invocation would issue real, billed logical requests and write real")
                print("   contracts.  A dry run never does that.  Broker startup and the pinned")
                print("   protocol were verified; re-run without --dry-run to proceed.")
            elif not supports_output:
                record["steps"].append({"step": "rehearsal_skipped", "at": utc_now(),
                                        "why": "this tree's finalizer has no --output"})
                print("\n-- rehearsal SKIPPED: this tree's finalizer has no --output, so a "
                      "rehearsal would overwrite the run's own aggregation.")
            else:
                print("\n-- rehearsal: re-running the tree finalizer to a scratch aggregation "
                      "(no case is judgeable, so no provider call is possible)")
                step = run_finalizer(finalizer_command(task_root, run_dir, credential, endpoint,
                                                       evidence_dir / "rehearsal_aggregation.json",
                                                       family), "rehearsal", evidence_dir)
                record["steps"].append(step)
                after = stats(endpoint)
                calls = broker_counter(after, "calls") - broker_counter(before_stats, "calls")
                step["broker_call_delta"] = calls
                print(f"   finalizer exit {step['returncode']}, provider calls {calls}")
                rehearsal_path = evidence_dir / "rehearsal_aggregation.json"
                if rehearsal_path.is_file():
                    rehearsal = read_json(rehearsal_path)
                    record["rehearsal_result_reasons"] = rehearsal.get("result_reasons")
                    for line in (rehearsal.get("result_reasons") or [])[:8]:
                        print("     reason: " + line)
                    print(f"   rehearsal publishable={rehearsal.get('formal_result_publishable')}")
                if calls != 0:
                    raise Refusal(f"rehearsal unexpectedly issued {calls} provider call(s)")
                current = contract_snapshot(run_dir)
                drift = sorted(case for case, digest in snapshot.items()
                               if current.get(case) != digest)
                record["contract_drift_after_rehearsal"] = drift
                if drift:
                    raise Refusal("the rehearsal changed existing contracts: " + ", ".join(drift))
                print("   no existing contract was modified")
            print("\n-- dry run complete; the run is unchanged")
            print("   real command: " + " ".join(
                finalizer_command(task_root, run_dir, credential, endpoint, None, family)))
            return 0

        # ---- real re-finalization ---------------------------------------
        print(f"\n-- re-running the tree finalizer ({len(would_judge)} new logical request(s) "
              "expected, one per unjudged case)")
        if args.shim_debug:
            debug_path = args.shim_debug.resolve()
            print(f"   traced through the tree's own adapter; debug -> {debug_path}")
            command = shim_debug_command(task_root, run_dir, credential, endpoint,
                                         debug_path, evidence_dir, None)
            record["shim_debug_path"] = str(debug_path)
            record["shim_debug_driver"] = str(evidence_dir / "shim_debug_driver.py")
        else:
            command = finalizer_command(task_root, run_dir, credential, endpoint, None, family)
        step = run_finalizer(command, "refinalize", evidence_dir)
        if args.shim_debug and debug_path.is_file():
            debug = read_json(debug_path)
            record["shim_debug"] = {k: debug[k] for k in
                                    ("exit_code", "driver_error", "frozen_digest_changed",
                                     "case_results_changed", "bundles_changed") if k in debug}
            print("   frozen-tree digest changed during the pass: "
                  + str(debug.get("frozen_digest_changed")))
            print("   hidden case_result.json changed: "
                  + (", ".join(debug.get("case_results_changed") or []) or "none"))
            print("   inputs bundles changed: "
                  + (", ".join(debug.get("bundles_changed") or []) or "none"))
            for case, entry in sorted((debug.get("cases") or {}).items()):
                if entry.get("error"):
                    print(f"   {case}: ADAPTER RAISED {entry['error']}")
                else:
                    print(f"   {case}: prepared, candidate_digest="
                          f"{entry.get('record_candidate_digest')} "
                          f"classification={entry.get('classification')}")
            missing = [c for c in CASES if c not in (debug.get("cases") or {})]
            if missing:
                print("   adapter never called for: " + ", ".join(missing))
        record["steps"].append(step)
        after = stats(endpoint)
        calls = broker_counter(after, "calls") - broker_counter(before_stats, "calls")
        step["broker_call_delta"] = calls
        print(f"   finalizer exit {step['returncode']}, provider calls {calls}")

        aggregation = read_json(aggregation_path)
        shutil.copy2(aggregation_path, evidence_dir / "formal_aggregation.post-refinalize.json")
        after_snapshot = contract_snapshot(run_dir)
        drift = sorted(case for case, digest in snapshot.items()
                       if after_snapshot.get(case) != digest)
        record["contract_drift"] = drift
        fresh = aggregation.get("fresh_semantic_judge_cases") or []
        record["fresh_semantic_judge_cases"] = fresh
        record["broker_call_delta"] = calls
        record["hidden_case_record_sha256_after"] = hidden_record_digests(run_dir)
        record["hidden_case_records_changed"] = sorted(
            case for case, digest in hidden_records_before.items()
            if record["hidden_case_record_sha256_after"].get(case) != digest)
        record["post_aggregation"] = {
            "formal_result_publishable": aggregation.get("formal_result_publishable"),
            "formal_complete": aggregation.get("formal_complete"),
            "result_axis": aggregation.get("result_axis"),
            "reasons": aggregation.get("reasons")}

        refinalization = {
            "schema_version": "agentswe-edit-formal-refinalize/v1",
            "reason": reason, "refinalized_at": utc_now(), "tool": str(TOOL_PATH),
            "finalizer_argv": step["command"], "finalizer_exit": step["returncode"],
            "broker": record["broker"], "broker_call_delta": calls,
            "cases_judged_in_this_pass": fresh,
            "pre_existing_contracts_unchanged": not drift,
            "pre_refinalize_aggregation": str(evidence_dir / "formal_aggregation.pre-refinalize.json"),
            "note": "no case directory was archived or edited; cases with a bound scoring_intent "
                    "were reused from disk, and each newly judged case made exactly one logical "
                    "request.",
        }
        aggregation["formal_refinalizations"] = (
            list(aggregation.get("formal_refinalizations") or []) + [refinalization])
        repairs = repairs_on_disk(run_dir)
        if repairs:  # the finalizer rewrote the file; earlier repairs must survive
            aggregation["result_judging_repairs"] = repairs
        write_json(aggregation_path, aggregation)

        if summary_path.is_file():
            summary = read_json(summary_path)
            summary.update({
                "result_axis": aggregation.get("result_axis"),
                "formal_result_claimed": bool(aggregation.get("formal_result_publishable")),
                "code_score_claimed": bool(aggregation.get("code_score_publishable")),
                "formal_finalizer_exit": step["returncode"],
                "formal_aggregation": "formal_aggregation.json",
                "formal_refinalizations": [refinalization],
                "result_judge_broker_owned_by_one_stop": False,
                "post_run_repair": {
                    "note": "these fields were refreshed from formal_aggregation.json by the "
                            "formal re-finalization tool, not by formal_one_stop; the original "
                            "`status` is left as the run recorded it",
                    "status_at_run_end": summary.get("status"),
                    "status_after_refinalize": ("formal_evidence_complete"
                                                if aggregation.get("formal_result_publishable")
                                                else summary.get("status")),
                    "reason": reason, "tool": str(TOOL_PATH), "at": utc_now(),
                    "pre_repair_copy": str(evidence_dir / "one_stop_summary.pre-refinalize.json")},
            })
            write_json(summary_path, summary)

        write_json(run_dir / "refinalize_record.json", record)
        print("\n== summary")
        print(f"   cases judged this pass : {fresh or 'none'}")
        print(f"   pre-existing contracts : {'UNCHANGED' if not drift else 'CHANGED: ' + ', '.join(drift)}")
        print(f"   aggregation            : publishable={aggregation.get('formal_result_publishable')}"
              f"  complete={aggregation.get('formal_complete')}")
        print(f"   result_axis            : "
              f"{json.dumps(aggregation.get('result_axis'), ensure_ascii=False)}")
        for line in aggregation.get("reasons") or []:
            print("   reason: " + line)
        print(f"   evidence               : {evidence_dir}")
        return 0 if aggregation.get("formal_result_publishable") and not drift else 3
    finally:
        record["finished_at"] = utc_now()
        try:
            record["broker_cleanup"] = broker.close()
            print(f"\n-- stopped broker {broker.name}: "
                  f"absent_after_cleanup={record['broker_cleanup'].get('absent_after_cleanup')}")
        except Exception as exc:  # noqa: BLE001
            record["broker_cleanup_error"] = f"{type(exc).__name__}: {exc}"
            print(f"\n!! broker cleanup error: {type(exc).__name__}: {exc}")
        write_json(evidence_dir / "refinalize_record.json", record)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        raise SystemExit(4)
