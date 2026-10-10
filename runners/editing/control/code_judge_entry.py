"""Create Code scoring with the user's single-logical-request Edit transport.

Keep Create prompt, source pack, eight dimensions and validator unchanged.
Only transport is replaced: one immutable logical request, explicit transient
retries, no resampling after a delivered response, and exact provider usage.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys

import result_judge as transport

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens')


def checked_usage(value):
    if (isinstance(value, dict)
            and all(type(value.get(k)) is int and value[k] >= 0 for k in TOKEN_FIELDS)
            and value['total_tokens'] >= value['input_tokens'] + value['output_tokens']):
        return {k: value[k] for k in TOKEN_FIELDS}
    return None


def account_usage(ledger, output, terminal_usage=None):
    """Account for every attempted HTTP request, including an unknown prefix."""
    attempts = ledger['transport_attempts']
    records = None
    path = output / 'code_provider_response-attempts.json'
    if path.is_file():
        try:
            value = json.loads(path.read_text())
            rows = value.get('attempts') if isinstance(value, dict) else None
            if (isinstance(rows, list) and type(attempts) is int and len(rows) == attempts
                    and all(isinstance(row, dict) and row.get('attempt') == index
                            for index, row in enumerate(rows, 1))):
                records = rows
        except (OSError, ValueError):
            pass
    known = {k: 0 for k in TOKEN_FIELDS}
    unknown = 0
    if records is not None:
        for row in records:
            usage = checked_usage(row.get('usage')) if row.get('usage_known') is True else None
            if usage is not None:
                for key in TOKEN_FIELDS:
                    known[key] += usage[key]
            elif row.get('error_type') == 'connect_timeout' and row.get('http_status') is None:
                # The shared transport classifies only connection-establishment
                # timeouts here; body/stream timeouts retain unknown billing.
                continue
            else:
                unknown += 1
        ledger['usage_evidence'] = str(path)
    else:
        usage = checked_usage(terminal_usage)
        if usage is not None and type(attempts) is int and attempts > 0:
            known.update(usage)
            unknown = attempts - 1
        else:
            unknown = attempts if type(attempts) is int else None
    ledger.update({f'known_{key}': value for key, value in known.items()})
    ledger['unknown_usage_attempts'] = unknown
    ledger['usage_complete'] = unknown == 0
    ledger.update({key: value if unknown == 0 else None for key, value in known.items()})


def load_create(path=Path('/judge/code_eval.py')):
    spec = importlib.util.spec_from_file_location('create_code_eval', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_transport(create, output, *, caller=None):
    caller = caller or transport.call_judge
    ledger = {'logical_requests': 0, 'transport_attempts': 0, 'completed_responses': 0,
              'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    account_usage(ledger, output)
    # Both roles use the same explicitly selected provider route order.
    os.environ['AGENTSWE_RESULT_JUDGE_ENDPOINTS'] = ','.join(create.ENDPOINTS)
    def call(prompt, api_key, timeout, *, transport_mode='stream'):
        if ledger['logical_requests']:
            raise RuntimeError('Edit forbids a second logical Code judgement; completed output is not resampled')
        with (output / 'code_logical_request_started.json').open('x') as handle:
            json.dump({'logical_requests': 1, 'model': create.MODEL,
                       'reasoning_effort': create.REASONING_EFFORT}, handle)
        ledger['logical_requests'] = 1
        ledger['transport_mode'] = transport_mode
        ledger['transport_attempts'] = None
        terminal_usage = None
        account_usage(ledger, output)
        transport.write_json(output / 'code_transport_ledger.json', ledger)
        try:
            text, attempts, endpoint, endpoints, usage = caller(
                prompt, api_key, timeout, min(4, create.MAX_ATTEMPTS),
                response_path=output / 'code_provider_response.json', transport_mode=transport_mode)
            ledger.update(transport_attempts=attempts, completed_responses=1)
            terminal_usage = usage
            return text, attempts, endpoint, endpoints
        except transport.TransportFailure as exc:
            ledger['transport_attempts'] = exc.attempts
            if isinstance(exc, transport.ReceivedResponseFailure):
                ledger['completed_responses'] = int(exc.completed)
                terminal_usage = exc.usage
            raise create.JudgeTransportError(str(exc), exc.attempts, exc.endpoints) from None
        finally:
            account_usage(ledger, output, terminal_usage)
            transport.write_json(output / 'code_transport_ledger.json', ledger)
    create.call_judge = call
    return ledger


def main():
    create = load_create()
    args = sys.argv[1:]
    def flag(name):
        return Path(args[args.index(name) + 1])
    output = flag('--output-dir')
    if os.environ.get('AGENTSWE_CODE_JUDGE_PREFLIGHT') == '1':
        evidence_paths = [args[index + 1] for index, name in enumerate(args) if name == '--evidence-path']
        source, _ = create.source_manifest_and_pack(flag('--candidate-source'),
                                                   max_pack_bytes=create.MAX_SOURCE_PACK_BYTES,
                                                   evidence_paths=evidence_paths)
        requirements, _ = create.source_manifest_and_pack(flag('--public-requirements'),
                                                        max_pack_bytes=create.MAX_REQUIREMENTS_PACK_BYTES)
        expected = args[args.index('--expected-candidate-digest') + 1] if '--expected-candidate-digest' in args else ''
        if expected and source['tree_digest'] != expected:
            raise ValueError('frozen source digest does not match the authoritative Create Code digest')
        if not flag('--code-rubric').is_file():
            raise ValueError('missing Code rubric')
        print(json.dumps({'preflight_valid': True, 'provider_calls': 0,
                          'candidate_digest': source['tree_digest'],
                          'requirements_digest': requirements['tree_digest']}))
        return 0
    if ((output / 'code_logical_request_started.json').exists()
            or (output / 'code_score_contract.json').exists()):
        print('previous Code logical request or contract exists; refusing to overwrite or resample', file=sys.stderr)
        return 2
    ledger = install_transport(create, output)
    code = create.main()
    contract_path = output / 'code_score_contract.json'
    if contract_path.is_file():
        contract = json.loads(contract_path.read_text())
        contract['create_internal_provider_counters'] = contract.get('provider_usage')
        contract['provider_usage'] = dict(ledger)
        valid_usage = (ledger['logical_requests'] == ledger['completed_responses'] == 1
                       and all(type(ledger[k]) is int and ledger[k] > 0
                               for k in ('input_tokens', 'output_tokens', 'total_tokens'))
                       and ledger['total_tokens'] >= ledger['input_tokens'] + ledger['output_tokens'])
        if not valid_usage:
            contract['contract_valid'] = contract['code_score_publishable'] = False
            contract['code_score'] = None
            contract.setdefault('errors', []).append('missing valid completed Code transport usage')
            code = 1
        contract['edit_transport_policy'] = {
            'single_logical_request': True, 'completed_response_resampling': False,
            'transport': 'Responses ' + ledger.get('transport_mode', 'not_requested') + ' with bounded explicit transient retries',
            'create_scoring_logic_modified': False,
            'ledger': str(output / 'code_transport_ledger.json')}
        transport.write_json(contract_path, contract)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
