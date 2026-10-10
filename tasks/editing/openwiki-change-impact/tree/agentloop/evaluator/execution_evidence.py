"""Task-native artifact authorship and conservative execution attribution."""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path


def broker_observation_reference(output, summary):
    """Keep repeated launcher/oracle metadata below the shared judge limits."""
    path = Path(output) / 'native-broker-observation.json'
    result = {key: summary.get(key) for key in (
        'schema_version', 'context_id', 'observer_source', 'complete', 'sealed',
        'pending', 'limits', 'counters', 'errors', 'record_references')}
    result.update(path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


def broker_trajectory(summary):
    """Project captured protocol fields without repeating request history.

    Exact duplicate Candidate-reported outputs retain all exchange references.
    Conflicting outputs are separate values. Neither is proof of execution.
    Full framed events remain in the independently hashed observation files.
    """
    exchanges, outputs, output_indices = [], [], {}
    for record in summary.get('records', []):
        item = {key: record.get(key) for key in (
            'sequence', 'path', 'sha256', 'context_id', 'request_sha256',
            'response_sha256', 'response_status', 'started_at', 'finished_at',
            'complete', 'model_status', 'model_tool_calls', 'assistant_messages')}
        item['reported_output_indices'] = []
        for value in record.get('candidate_reported_tool_outputs', []):
            encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
            identity = hashlib.sha256(encoded).hexdigest()
            if identity not in output_indices:
                output_indices[identity] = len(outputs)
                outputs.append({'value': value, 'observed_in_exchanges': []})
            index = output_indices[identity]
            outputs[index]['observed_in_exchanges'].append(record.get('sequence'))
            item['reported_output_indices'].append(index)
        exchanges.append(item)
    return {'exchanges': exchanges, 'candidate_reported_outputs': outputs,
        'interpretation': 'Outputs in requests are untrusted Candidate reports. Model calls are actual broker response fields. Match them with OS writes, process exits and artifact evidence before inferring execution.'}


def judge_input_sizes(output):
    """Check actual serialized files against the shared judge's fixed limits.

    Preserve all evidence bytes. Oversize evidence is an evaluator failure;
    neither truncation nor an unobserved Candidate zero repairs that failure.
    """
    output = Path(output)
    paths = {
        'task_input': (output / 'executed_task.md', 500_000),
        'rubric': (Path(__file__).with_name('result_rubric.md'), 500_000),
        'agent_artifact': (native_artifact_path(output / 'workspace'), 1_500_000),
        'trajectory': (output / 'observed_trajectory.json', 2_500_000),
        'native_evidence': (output / 'launcher_result.json', 2_500_000),
        'oracle_summary': (output / 'private-oracle-comparison.json', 1_500_000),
    }
    inputs = {}
    for name, (path, limit) in paths.items():
        size = path.stat().st_size if path.is_file() else None
        inputs[name] = {'path': str(path), 'bytes': size, 'limit_bytes': limit,
            'within_limit': size is not None and size <= limit}
    return {'schema_version': 'openwiki-result-input-sizes/v1',
        'valid': all(item['within_limit'] for item in inputs.values()),
        'evidence_truncated': False, 'inputs': inputs}


def validate_execution_context(output, repository, request, case_id, resources):
    from ..protocol import tree_digest, file_sha256
    shared = '@@AGENTSWE_EDITING_CONTROL@@'
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from validate_formal_config import tree_digest as task_tree_digest
    output, repository = Path(output), Path(repository)
    context = json.loads((output / 'logical-context.json').read_text())
    fields = {k:v for k,v in context.items() if k != 'context_id'}
    if context.get('context_id') != hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest():
        raise ValueError('process context digest mismatch')
    expected = {'candidate_digest':tree_digest(repository),
        'task_source_digest':task_tree_digest(Path(__file__).resolve().parents[2]),
        'product_entry_sha256':file_sha256(repository / 'dist/cli.js'),
        'request_sha256':file_sha256(Path(request)), 'case_id':case_id,
        'output_path':str(output.resolve()), 'workspace_path':str((output / 'workspace').resolve())}
    if any(context.get(k) != v for k,v in expected.items()):
        raise ValueError('observed product/source/request/path identity mismatch')
    cgroups = [line.split(':', 2)[2] for line in context.get('resource_cgroup','').splitlines() if line.startswith('0::')]
    if (resources.get('valid') is not True or cgroups != [resources.get('cgroup')]
            or not 0 < context.get('remaining_case_timeout_seconds', 0) <= resources.get('timeout_seconds', 0)):
        raise ValueError('product execution was not inside the trusted case resource scope')
    return context


def native_artifact_path(workspace):
    """Locate an actual product file; never synthesize or select a best answer.

    The contract path wins whenever present. The native CLI confines write_file
    to /openwiki; its single alternate result path is scoreable partial evidence
    and retains a path-compliance finding for the independent Result judge.
    """
    workspace=Path(workspace)
    preferred=workspace/'agent_result.json'
    if preferred.exists() or preferred.is_symlink():return preferred
    alternate=workspace/'openwiki/agent_result.json'
    if not (workspace/'openwiki').is_symlink() and alternate.is_file() and not alternate.is_symlink():return alternate
    return preferred


def _terminal_json_object(text):
    """Find the trailing JSON object in one pass over bounded CLI output."""
    text = text.rstrip()
    if not text.endswith('}'):
        return None
    depth, in_string = 0, False
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if char == '"':
            previous, slashes = index - 1, 0
            while previous >= 0 and text[previous] == '\\':
                previous -= 1
                slashes += 1
            if slashes % 2 == 0:
                in_string = not in_string
        elif not in_string:
            if char == '}':
                depth += 1
            elif char == '{':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[index:])
                    except (ValueError, RecursionError):
                        return None
    return None


def _model_answer_texts(output):
    """Model answers the evaluator itself relayed, from its own broker record."""
    try:
        summary = json.loads((Path(output) / 'native-broker-observation.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    texts = []
    for record in (summary.get('records') or []):
        if not isinstance(record, dict):
            continue
        for message in (record.get('assistant_messages') or []):
            text = message.get('text') if isinstance(message, dict) else None
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return texts


def _quotes_model_text(artifact, answers, *, window=60):
    """True when the delivered artifact repeats a relayed model answer verbatim.

    0921: the 0920 model-call rule is decided by the broker call delta alone, so
    the cheapest compliant product asked the model almost nothing and pasted the
    reply into `observations`/`decision.rationale` -- which input/02 already
    reserves for the product's own tool/CLI trace, and which now also states that
    the model answer must be consumed into the maintenance.  Deliberately
    conservative: only a contiguous 60-character run taken from the start of an
    answer at least that long counts, so a product that reads the answer and
    writes its own observation, or that quotes a short identifier, never trips it.
    """
    if not isinstance(artifact, dict):
        return False
    fields = []
    observations = artifact.get('observations')
    if isinstance(observations, list):
        fields.extend(str(item) for item in observations)
    decision = artifact.get('decision')
    if isinstance(decision, dict):
        fields.append(str(decision.get('rationale') or ''))
    haystack = ' \u241f '.join(' '.join(str(item).split()) for item in fields)
    for answer in answers:
        needle = ' '.join(str(answer).split())[:window]
        if len(needle) >= window and needle in haystack:
            return True
    return False


def _existing_paths(*candidates):
    """Cite only attribution evidence that is on disk.

    D49: `execution_contract.py` digests every path in
    `failure_attribution.evidence_paths` and answers `unresolved` when one cannot
    be read.  A path that names a file the evaluator never wrote is therefore a
    veto on the Candidate attribution, not evidence for it.
    """
    paths: list[str] = []
    for item in candidates:
        if not item:
            continue
        text = str(item)
        if text not in paths and Path(text).is_file():
            paths.append(text)
    return paths


def artifact_authorship(output, case_id, *, preexisting=False):
    output = Path(output)
    path, stdout = native_artifact_path(output/'workspace'), output / 'stdout.log'
    result = {'validated_by': 'evaluator', 'valid': False, 'sha256': None,
        'artifact_path': str(path), 'raw_trajectory': str(stdout), 'artifact_preexisting': preexisting}
    if not path.is_file() and not path.is_symlink():
        return result
    if path.is_symlink():
        result['substantive_native_artifact'] = True
        return result
    try:
        raw = path.read_bytes()
        result['sha256'] = hashlib.sha256(raw).hexdigest()
        # Missing stdout is missing authorship evidence; it is never proof
        # that the product left no substantive artifact. Preserve this fact
        # before parsing, so malformed nonempty output is not erased either.
        result['substantive_native_artifact'] = bool(raw.strip())
        artifact = json.loads(raw)
        substantive = (isinstance(artifact, dict) and any(value not in (None, '', [], {}) for key,value in artifact.items()
            if key not in {'schema_version','case_id','artifact_owner','evaluator_synthesized'})) or (not isinstance(artifact, dict) and bool(artifact))
        result['substantive_native_artifact'] = substantive
        if preexisting or not stdout.is_file():
            return result
        schema_ok = isinstance(artifact, dict) and artifact.get('schema_version') == 'openwiki-agent-result/v1' \
            and artifact.get('case_id') == case_id and isinstance(artifact.get('observations'), list) \
            and isinstance(artifact.get('integrity'), dict) and isinstance(artifact.get('decision'), dict) \
            and isinstance(artifact['decision'].get('completion_claim'), str)
        text = stdout.read_text()
        # CLI --print concatenates model text and emits it to stdout. Require
        # the exact final JSON value, not an evaluator's synthetic event label.
        terminal = _terminal_json_object(text)
        findings=[]
        if path != output/'workspace/agent_result.json':findings.append('native artifact was written at openwiki/agent_result.json instead of the requested root path')
        if not schema_ok:findings.append('artifact schema or claimed case fields are incomplete or inconsistent')
        # 0920: name the authorship rule the Builder was previously failing blind.
        # `valid` is unchanged -- only the reported reason is, so dev feedback can
        # state which half of the contract broke instead of only the schema half.
        if terminal != artifact:findings.append('final production CLI stdout does not end with the exact agent_result.json object; the --print run must emit the finished result object verbatim as its last JSON value')
        # 0921: name the model-answer-consumption half of the same contract.
        if _quotes_model_text(artifact, _model_answer_texts(output)):
            findings.append('the result artifact repeats a relayed model answer verbatim in observations or decision.rationale; the model answer must be consumed into the maintenance decision, and observations must carry the product own tool/CLI trace')
        result.update(valid=substantive and terminal == artifact,
            substantive_native_artifact=substantive,format_valid=schema_ok,
            quality_findings=findings,terminal_response_matches=terminal == artifact)

    except (OSError, ValueError, UnicodeError):
        pass
    return result


def attest(run, *, case_id, candidate_digest, output):
    output = Path(output)
    record = dict(run, case_id=case_id, candidate_digest=candidate_digest)
    native = output / 'launcher_result.json'
    preflight = output / ('native-transport-health.json' if run.get('timeout_start_recovered') is True
                          else 'transport_preflight.json')
    try:
        transport = json.loads(preflight.read_text())
    except (OSError, ValueError):
        transport = {}
    healthy = bool(transport.get('valid')) and not transport.get('endpoint_mapping_errors') \
        and not run.get('transport_errors') and not run.get('infrastructure_invalid')
    record.update(environment_preflight={'valid': healthy, 'evidence_path': str(preflight)},
        execution_attempted=run.get('product_started') is True,
        real_execution=run.get('product_started') is True and int(run.get('broker_successful_calls', 0)) > 0,
        artifact_validation=artifact_authorship(output, case_id, preexisting=run.get('artifact_preexisting', False)))
    if not healthy:
        record.update(classification='infrastructure_invalid', infra_valid=False, infrastructure_invalid=True)
        record['failure_attribution'] = {'party': 'evaluator', 'observed_by': 'evaluator',
            'reason': 'isolated runtime/endpoint preflight or broker transport did not establish valid execution'}
        return record
    validation=record['artifact_validation']
    # D49 (owner policy, 2026-09-21): a case the evaluator killed at its own deadline is
    # a Candidate outcome.  It scores 0 and it consumes the round, on the dev path and on
    # the hidden path alike.  Decide it here, before any artifact branch can reach for
    # `stdout.log`: `lower_agent_launcher.py` returns from its `TimeoutExpired` branch
    # before the line that writes that file, so the branch below used to hand the shared
    # contract an evidence path that does not exist, `execution_contract.py:93-96`
    # answered `unresolved`, and `controller.py:551-556` then made the round
    # non-consuming -- the Builder resubmits the same product until its budget is gone
    # (0921-v4-001 on 178: four attempts at round 4, 99 minutes, the cell never froze).
    if ((run.get('candidate_classification') == 'candidate_timeout'
         or (run.get('case_resource_contract') or {}).get('timed_out') is True)
            and run.get('product_started') is True):
        evidence = _existing_paths(native, output / 'stdout.log', output / 'stderr.log',
                                   output / 'case_resources/resource-attestation.json',
                                   validation.get('artifact_path'))
        record.update(classification='candidate_timeout', infra_valid=True,
                      infrastructure_invalid=False)
        record['quality_findings'] = validation.get('quality_findings') or []
        record['failure_attribution'] = {'party': 'candidate', 'observed_by': 'evaluator',
            'fatal': True,
            'reason': 'the case deadline expired with the OpenWiki product still running and '
                      'the evaluator killed it (elapsed %s s of the case budget); a timed-out '
                      'case is a Candidate outcome: it scores 0 and it consumes the round'
                      % run.get('elapsed_seconds'),
            'evidence_paths': evidence or [str(native)]}
        return record
    if validation['valid'] and record['real_execution']:
        if run.get('semantic_observer_in_case_budget') is not True:
            record.update(classification='infrastructure_invalid', infra_valid=False, infrastructure_invalid=True,
                failure_attribution={'party': 'evaluator', 'observed_by': 'evaluator',
                    'reason': 'case-budgeted native semantic observation is missing for a substantive partial artifact'})
            return record
        record['classification'] = 'candidate_product_success'
        record['ordinary_partial_artifact_scoreable']=bool(validation.get('quality_findings'))
        record['quality_findings']=validation.get('quality_findings',[])
        return record
    if validation.get('substantive_native_artifact'):
        if record.get('real_execution'):
            # The Candidate ran and wrote a real artifact; the evaluator read it
            # and recorded exactly how it breaks the contract. That is a
            # Candidate outcome, and it has to consume the round -- attributing
            # it to the evaluator makes the round non-consuming and the same
            # candidate is retried until the Builder runs out of time.
            findings = validation.get('quality_findings') or []
            evidence = _existing_paths(validation.get('artifact_path'),
                                       validation.get('raw_trajectory'))
            record.update(classification='candidate_artifact_failure',
                infra_valid=True, infrastructure_invalid=False)
            record['quality_findings'] = findings
            record['failure_attribution'] = {'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': 'substantive native artifact does not satisfy the case artifact contract: '
                          + ('; '.join(str(f) for f in findings) or 'unspecified contract violation'),
                'evidence_paths': evidence or [str(native)]}
            return record
        if run.get('product_started') is True and 'exit_code' in run:
            # 2026-09-20 (readiness 0920-hd-001): the product started under a healthy
            # preflight, made NO model call, wrote an artifact and exited on its own.
            # Nothing evaluator-side failed: a product that answers without consulting
            # the model is a Candidate outcome. Booking it as evaluator failure made the
            # round non-consuming and the Builder resubmitted the same product eight
            # times without ever seeing its stderr.
            stderr_path = output / 'stderr.log'
            try:
                stderr_tail = stderr_path.read_text(encoding='utf-8', errors='replace')[-400:].strip()
            except OSError:
                stderr_tail = ''
            evidence = _existing_paths(native, validation.get('artifact_path'), stderr_path)
            record.update(classification='candidate_behavior_failure', infra_valid=True, infrastructure_invalid=False)
            record['failure_attribution'] = {'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': 'product wrote a substantive artifact without any model call; native authorship is '
                          'not established (exit_code=%s; product stderr tail: %s)' % (run.get('exit_code'), stderr_tail or '<empty>'),
                'evidence_paths': evidence}
            return record
        record.update(classification='infrastructure_invalid',infra_valid=False,infrastructure_invalid=True,
            failure_attribution={'party':'evaluator','observed_by':'evaluator',
                'reason':'substantive product artifact exists but native authorship evidence is incomplete'})
        return record
    # (the former `case_resource_contract.timed_out` branch moved above, ahead of the
    # artifact branches, and now also covers a launcher-observed `candidate_timeout`)
    observed = native.is_file() and run.get('product_started') is True and ('exit_code' in run or run.get('classification') == 'candidate_timeout')
    if observed:
        record.update(classification='candidate_behavior_failure')
        record['failure_attribution'] = {'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
            'reason': 'actual OpenWiki process terminated without a substantive native case artifact',
            'evidence_paths': _existing_paths(native, preflight) or [str(native)]}
    return record


def disconnect_only_incomplete(broker_observation, run):
    """True when a sealed broker observation is incomplete only through the Candidate reply disconnects the launcher
    tolerated (lower_agent_launcher._candidate_reply_disconnect): every other exchange complete, no store error."""
    disconnect = run.get('candidate_reply_disconnect') or {}
    tolerated = {item.get('observation_path') for item in disconnect.get('disconnected_requests') or []
                 if isinstance(item, dict) and item.get('observation_path')}
    if (not tolerated or broker_observation.get('errors') or broker_observation.get('sealed') is not True
            or int(broker_observation.get('pending', 0) or 0)):
        return False
    incomplete = {record.get('path') for record in broker_observation.get('records') or []
                  if not isinstance(record, dict) or record.get('complete') is not True}
    return bool(incomplete) and incomplete <= tolerated


def prepare_evidence(run, *, case_id, candidate_digest, repository, output, request, cases_root, initial):
    from .semantic_oracle import observe
    from ..protocol import file_sha256, write_json
    output = Path(output)
    task = output / 'executed_task.md'
    # This is byte-for-byte the actual argv task read by command(), including
    # runtime JSON. Do not reconstruct an approximately equivalent Markdown.
    task.write_bytes(Path(request).read_bytes())
    comparison = output / 'private-oracle-comparison.json'
    native_path = output / 'native-semantic-comparison.json'
    if run.get('semantic_observer_in_case_budget') is True:
        if not native_path.is_file() or hashlib.sha256(native_path.read_bytes()).hexdigest() != run.get('native_semantic_comparison_sha256'):
            raise ValueError('case-budgeted semantic observation is missing or changed')
        value = json.loads(native_path.read_text())
        if value.get('case_id') != case_id:
            raise ValueError('case-budgeted observation case identity mismatch')
    else:
        # A failed driver may not reach observation; no post-timeout solution
        # work or invented observations can rescue that missing evidence.
        value = {'case_id': case_id, 'observation_unavailable': True, 'semantic_comparisons': [],
            'candidate_visible': False,
            'private_oracle_not_candidate_visible': True, 'observer_not_executed_after_case_deadline': True}
    value.update(executed_task_path=str(task), executed_task_sha256=file_sha256(task),
        candidate_digest=candidate_digest)
    record = attest(run, case_id=case_id, candidate_digest=candidate_digest, output=output)
    observation = run.get('native_process_observation', {})
    broker_observation = run.get('native_broker_observation', {})
    shared = '@@AGENTSWE_EDITING_CONTROL@@'
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from execution_contract import classify_candidate_execution
    preliminary = classify_candidate_execution(record, case_id=case_id,
        candidate_digest=candidate_digest)
    # An independently evidenced fatal Candidate timeout already has a
    # deterministic zero. It must retain incomplete traces as such without
    # requiring a semantic judge or changing that zero into infrastructure.
    requires_observation = preliminary['classification'] == 'scoreable'
    record['observation_required_for_result_judge'] = requires_observation
    if requires_observation:
        from .process_observation import load_observation
        from .broker_observation import load_observations
        try:
            context = validate_execution_context(output, repository, task, case_id,
                run.get('case_resource_contract', {}))
            observation = load_observation(output, context)
            summary_path = output / 'native-broker-observation.json'
            expected_broker_ref = run.get('native_broker_observation', {})
            if (expected_broker_ref.get('path') != str(summary_path)
                    or expected_broker_ref.get('sha256') != file_sha256(summary_path)):
                raise ValueError('broker summary changed after launcher sealed its reference')
            broker_observation = load_observations(output, context['context_id'])
            if observation.get('complete') is not True:
                raise ValueError('native process observation incomplete')
            if ((broker_observation.get('complete') is not True
                    and not disconnect_only_incomplete(broker_observation, run))
                    or not broker_observation.get('records')):
                raise ValueError('native broker observation incomplete or missing successful exchange')
        except (KeyError, OSError, TypeError, ValueError) as exc:
            record.update(classification='infrastructure_invalid', infra_valid=False,
                infrastructure_invalid=True, failure_attribution={'party':'evaluator',
                    'observed_by':'evaluator', 'reason':str(exc)})
    broker_reference = broker_observation_reference(output, broker_observation)
    value.update(native_process_observation=observation, native_broker_observation=broker_reference,
        execution_observation_policy='The separate raw trajectory contains evaluator-captured OS calls, writes and exits. The oracle itself did not execute any example or search query. Interpret semantic success only from observed execution and actual artifacts.')
    write_json(comparison, value)
    trajectory = output / 'observed_trajectory.json'
    raw = output / 'native-process.trace'
    stdout = output / 'stdout.log'
    write_json(trajectory, {'schema_version':'openwiki-observed-trajectory/v1',
        'native_cli_stdout_reference':{'path':str(stdout),
            'sha256':file_sha256(stdout) if stdout.is_file() else None},
        'native_cli_stdout_owner':'Candidate product; not proof of tool execution by itself',
        'broker_observation':broker_reference,
        'broker_trajectory':broker_trajectory(broker_observation),
        'broker_observations_owner':'evaluator relay; candidate reported tool outputs remain untrusted',
        'process_observation':observation,
        'native_process_trace':raw.read_text(errors='replace') if raw.is_file() else None,
        'semantic_policy':'Use observed execve, writes, exits and corresponding product artifacts. A command string, a refusal, or missing/truncated output does not establish successful example/search execution. No evaluator search or example was run.'})
    if requires_observation:
        sizes = judge_input_sizes(output)
        record['result_input_sizes'] = sizes
        if not sizes['valid']:
            invalid = [name for name, item in sizes['inputs'].items() if not item['within_limit']]
            record.update(classification='infrastructure_invalid', infra_valid=False,
                infrastructure_invalid=True, failure_attribution={'party': 'evaluator',
                    'observed_by': 'evaluator',
                    'reason': 'shared Result input missing or exceeds byte limit: ' + ', '.join(invalid)})
    record.update(executed_task_path=str(task), executed_task_sha256=file_sha256(task),
        native_process_observation=observation, native_broker_observation=broker_reference,
        executed_repository_digest=context.get('candidate_digest') if 'context' in locals() else None,
        observed_trajectory_path=str(trajectory), observed_trajectory_sha256=file_sha256(trajectory),
        private_oracle_comparison_path=str(comparison), private_oracle_comparison_sha256=file_sha256(comparison),
        execution_record_path=str(output / 'execution_record.json'))
    write_json(output / 'execution_record.json', record)
    return record
