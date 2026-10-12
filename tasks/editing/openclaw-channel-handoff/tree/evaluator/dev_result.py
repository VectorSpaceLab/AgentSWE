"""Independent, immutable public-dev Result scoring for OpenClaw.

No Candidate code runs here. Native evidence is captured before this module is
called; the model sees the actual artifact and sanitized evaluator observations.
An incomplete judge attempt is preserved and never automatically resampled.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from controller.dev_feedback import semantic_feedback
from evaluator.semantic_finalize import is_infrastructure, sanitized
from evaluator.durable_state import bound_durable_evidence
from evaluator.candidate_outcome import candidate_zero_receipt, semantic_admitted

JUDGE = Path('@@AGENTSWE_EDITING_CONTROL@@/result_judge.py')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


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


def score_public_case(*, root, candidate_digest, record, case_output, broker_endpoint):
    """Return native record plus a digest-bound semantic receipt, not a heuristic."""
    record = dict(record)
    case_id = record.get('case_id')
    if case_id not in {'dev_001', 'dev_002'}:
        raise ValueError('public scorer accepts only the declared dev inventory')
    output = Path(case_output).resolve() / 'semantic_result'
    receipt_path = output / 'receipt.json'

    def unavailable(reason):
        record['semantic_result'] = {'case_id': case_id, 'candidate_digest': candidate_digest,
            'classification': 'infrastructure-invalid', 'reason': reason}
        return record

    if is_infrastructure(record) or record.get('frozen_candidate_digest_stable') is not True:
        return unavailable('native execution or Candidate integrity is unresolved')
    if not bound_durable_evidence(record,case_id):
        return unavailable('independent stopped-scope durable evidence is unavailable')
    try:
        zero=candidate_zero_receipt(root=root,candidate_digest=candidate_digest,record=record,
            case_output=case_output,output=output)
        if zero is not None:
            record['semantic_result']=zero
            if not semantic_feedback(record,case_id,candidate_digest)['valid']:
                return unavailable('terminal Candidate failure evidence is invalid')
            return record
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return unavailable('terminal Candidate failure attribution unavailable: '+type(exc).__name__)
    # Strict delivery-schema quality is evidence for the semantic judge, not
    # a fatal gate. New partial admissions retain the original authored bytes.
    if ((record.get('artifact_contract', {}).get('valid') is not True and not semantic_admitted(record))
            or record.get('agent_authored_artifact') is not True):
        return unavailable('artifact/failure attribution requires evaluator review')
    try:
        artifact = Path(record['authored_artifact']).resolve()
        artifact.relative_to(Path(case_output).resolve())
        if sha(artifact) != record['artifact_sha256']:
            return unavailable('Agent artifact changed after native capture')
        if receipt_path.is_file():
            record['semantic_result'] = json.loads(receipt_path.read_text())
            if not semantic_feedback(record, case_id, candidate_digest)['valid']:
                return unavailable('saved semantic evidence has changed or is invalid')
            return record
        if output.exists():
            return unavailable('prior logical judge attempt requires recovery; no resampling')
        output.mkdir(parents=True)
        source_trajectory = Path(case_output) / 'trajectory.json'
        trajectory = json.loads(source_trajectory.read_text())
        native_case = record.get('native_case')
        if (not isinstance(native_case, dict) or native_case.get('case_id') != case_id
                or not isinstance(native_case.get('oracle_observations'), dict)):
            return unavailable('native task observations are unavailable')
        # No hidden case inventory or private facts file enters the request.
        # The observer's comparison outcomes are evidence, not authored claims.
        trajectory_path, native_path, oracle_path = [output / name for name in
            ('trajectory.json', 'native_evidence.json', 'oracle_summary.json')]
        write_new(trajectory_path, sanitized(trajectory))
        write_new(native_path, sanitized({k: record[k] for k in (
            'case_id', 'classification', 'production_entry', 'model', 'reasoning_effort',
            'broker_stats_delta', 'artifact_contract', 'native_case', 'durable_state') if k in record}))
        write_new(oracle_path, {'case_id': case_id, 'comparison_only': True,
            'observations': sanitized(native_case['oracle_observations']),
            'durable_state': {'collection_valid':True,
                'database_present':record['durable_state'].get('database_present'),
                'source_bytes_unchanged':record['durable_state'].get('source_bytes_unchanged'),
                'observations_location':'native_evidence.json/durable_state/observations'},
            'limitations': ['Durable fields were read independently; stored claims alone do not prove '
                'fault causality, correct authorization, or complete recovery. Compare actual external receipts '
                'and Agent behavior; absence of an observation is not proof of successful recovery.']})
        inputs = {'task_input': Path(root) / 'dev_cases' / case_id / 'input.md',
            'rubric': Path(root) / 'evaluator/result_rubric.md',
            'rubric_dimensions': Path(root) / 'evaluator/result_dimensions.json',
            'agent_artifact': artifact, 'trajectory': trajectory_path,
            'native_evidence': native_path, 'oracle_summary': oracle_path}
        # Dev and hidden must reach the Result judge under the SAME ceiling contract.
        # semantic_finalize.py:464-478 (formal) and readiness_smoke.py:277-286
        # (readiness) already issue evaluator/result_score_caps.py; the dev path did
        # not, so the Builder calibrated against an uncapped judge while the identical
        # evidence capped the hidden judge.  The contract is built from the very bytes
        # handed to the judge (rubric / native_evidence / oracle_summary), which is what
        # the shared judge's load_score_caps() input binding re-checks.  Putting it in
        # `inputs` makes it both a '--score-cap-contract' argument and a sha256-bound
        # entry of intent.json and binding.json, so it is immutable for this submission
        # and semantic_feedback() re-verifies it on every read.
        # A defect here must never make a round unpublishable: it degrades to the
        # prose-only rubric and records why, exactly as the hidden path does.
        cap_unavailable = None
        try:
            from evaluator.result_score_caps import enabled as caps_enabled, write_contract
            if caps_enabled():
                inputs['score_cap_contract'] = write_contract(
                    output / 'score_cap_contract.json', case_id, record,
                    rubric=inputs['rubric'], native_evidence=native_path,
                    oracle_summary=oracle_path,
                    # native_path above is the FLAT dev projection, not the hidden
                    # {facts: {...}} envelope, so the refs must not carry the facts hop.
                    ref_prefix='native_evidence.json')
        except Exception as exc:
            inputs.pop('score_cap_contract', None)
            cap_unavailable = '%s: %s' % (type(exc).__name__, exc)
        identities = {key: {'path': str(path.resolve()), 'sha256': sha(path)}
                      for key, path in inputs.items()}
        write_new(output / 'intent.json', {'case_id': case_id,
            'candidate_digest': candidate_digest, 'inputs': identities,
            'score_cap_contract_unavailable': cap_unavailable,
            'judge_source_sha256': sha(JUDGE), 'logical_requests_maximum': 1})
        command = ['/usr/bin/python3', '-B', str(JUDGE), '--case-id', case_id,
            '--broker-endpoint', broker_endpoint, '--output-dir', str(output / 'judge')]
        for key, path in inputs.items():
            command += ['--' + key.replace('_', '-'), str(path)]
        def rejudge():
            return subprocess.run(command, capture_output=True, text=True, check=False)

        completed = rejudge()
        again = resample_refused_verdict(output / 'judge', rejudge)
        if again is not None:
            completed = again
        # write_new() uses open('x'); the process record is written once, after
        # any resample, so it always describes the attempt that produced the
        # contract read below.
        write_new(output / 'judge_process.json', {'exit_code': completed.returncode,
            'entry': str(JUDGE), 'stderr': sanitized(completed.stderr[-4000:])})
        contract_path = output / 'judge/result_score_contract.json'
        if completed.returncode != 0 or not contract_path.is_file():
            return unavailable('Result judge did not complete; retain execution and logical request evidence')
        binding_path = output / 'binding.json'
        binding = {'case_id': case_id, 'candidate_digest': candidate_digest,
            'contract_sha256': sha(contract_path), 'inputs': identities}
        write_new(binding_path, binding)
        record['semantic_result'] = {'case_id': case_id, 'candidate_digest': candidate_digest,
            'classification': 'scoreable', 'contract_path': str(contract_path),
            'contract_sha256': sha(contract_path), 'binding_path': str(binding_path),
            'binding_sha256': sha(binding_path)}
        if not semantic_feedback(record, case_id, candidate_digest)['valid']:
            return unavailable('Result contract failed task-local dimensions, identity, or usage validation')
        write_new(receipt_path, record['semantic_result'])
        return record
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return unavailable('Result preparation failed: ' + type(exc).__name__)
