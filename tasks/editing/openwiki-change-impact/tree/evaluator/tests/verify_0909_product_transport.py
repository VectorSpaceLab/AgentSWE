"""Actual baseline OpenWiki CLI/SDK under mock API: transport, not score."""
from __future__ import annotations
import argparse
import http.server
import json
from pathlib import Path
import sys
import threading
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.fixture_service import runtime_case_paths
from agentloop.evaluator.lower_agent_launcher import run_case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--through-broker',action='store_true')
    parser.add_argument('--exercise-tool',action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    requests, reject = [], [False]
    stats = {'schema_version': 2, 'broker_instance_id': 'offline-openwiki-product-0909',
        'model': 'gpt-5.6-sol', 'reasoning_effort': 'high', 'credential_value_recorded': False,
        'calls': 0, 'successful_calls': 0, 'failures': 0, 'broker_failures': 0, 'provider_failures': 0}
    artifact = {'schema_version': 'openwiki-agent-result/v1', 'case_id': 'test_001',
        'observations': [{'kind': 'mock-only', 'observation': 'No benchmark work claimed'}],
        'integrity': {'mocked_provider': True}, 'decision': {'completion_claim': 'incomplete'}}
    terminal = json.dumps(artifact)
    class Mock(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def send_bytes(self, body, *, status=200, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.send_bytes(json.dumps(stats if self.path == '/stats' else {'ok': True}).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'path': self.path, 'model': body.get('model'), 'stream': body.get('stream'),'tool_names':[t.get('name') or t.get('function',{}).get('name') for t in body.get('tools',[])], 'tool_return_seen':any(item.get('type')=='function_call_output' and 'change_source' in json.dumps(item.get('output','')) for item in body.get('input',[]) if isinstance(item,dict))})
            stats['calls'] += 1
            if reject[0] or len(requests) > 8:
                stats['failures'] += 1
                stats['broker_failures'] += 1
                self.send_bytes(b'{"error":{"message":"unsupported_endpoint","type":"unsupported_endpoint"}}', status=404)
                return
            stats['successful_calls'] += 1
            if self.path == '/v1/chat/completions':
                response = {'id': 'chatcmpl_offline', 'object': 'chat.completion', 'created': int(time.time()), 'model': 'gpt-5.6-sol',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': terminal}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
                if body.get('stream'):
                    chunks = [dict(response, object='chat.completion.chunk', choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': terminal}, 'finish_reason': None}]),
                        dict(response, object='chat.completion.chunk', choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
                    self.send_bytes((''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode(), content_type='text/event-stream')
                else:
                    self.send_bytes(json.dumps(response).encode())
                return
            message = {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': terminal, 'annotations': []}]}
            if args.exercise_tool and len(requests)==1:
                message={'id':'fc_scripted_read','type':'function_call','call_id':'call_scripted_read','name':'read_file','arguments':json.dumps({'file_path':'/impact-manifest.json'}),'status':'completed'}
            response = {'id': 'resp_offline_'+str(len(requests)), 'object': 'response', 'created_at': int(time.time()), 'status': 'completed',
                'model': 'gpt-5.6-sol', 'output': [message], 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
            if body.get('stream'):
                events = [
                    {'type': 'response.created', 'response': dict(response, status='in_progress', output=[])},
                    {'type': 'response.output_item.added', 'output_index': 0, 'item': dict(message, status='in_progress', content=[])},
                    {'type': 'response.content_part.added', 'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                    {'type': 'response.output_text.delta', 'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'delta': terminal},
                    {'type': 'response.output_text.done', 'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'text': terminal},
                    {'type': 'response.content_part.done', 'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'part': message.get('content',[{'type':'output_text','text':'','annotations':[]}])[0]},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                    {'type': 'response.completed', 'response': response}]
                if message['type']=='function_call':
                    events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])},
                        {'type':'response.output_item.added','output_index':0,'item':dict(message,arguments='')},
                        {'type':'response.function_call_arguments.delta','output_index':0,'item_id':message['id'],'delta':message['arguments']},
                        {'type':'response.function_call_arguments.done','output_index':0,'item_id':message['id'],'arguments':message['arguments']},
                        {'type':'response.output_item.done','output_index':0,'item':message},
                        {'type':'response.completed','response':response}]
                self.send_bytes(''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(dict(event, sequence_number=i)) + '\n\n' for i, event in enumerate(events)).encode(), content_type='text/event-stream')
            else:
                self.send_bytes(json.dumps(response).encode())
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Mock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1/responses'
    broker_server=None
    if args.through_broker:
        from agentloop.evaluator.broker import BrokerState,handler
        secret=args.output/'synthetic-credential.env';secret.write_text('OPENAI_API_KEY=synthetic-no-provider\n')
        broker_state=BrokerState(secret,args.output/'broker-stats.json',broker_instance_id='native-local-probe')
        broker_server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler(broker_state,endpoint))
        threading.Thread(target=broker_server.serve_forever,daemon=True).start()
        endpoint=f'http://127.0.0.1:{broker_server.server_port}/v1/responses'
    try:
        fixture = runtime_case_paths('test_001', ROOT / 'test_cases', args.output / 'fixture')
        baseline = ROOT / '.runtime/candidate-smoke/repository'
        positive = run_case(baseline, ROOT / 'test_cases/test_001/input.md', args.output / 'positive', endpoint,
            timeout=90, working_directory=fixture['repository'],fixture_case_id='test_001',fixture_cases_root=ROOT/'test_cases',fixture_output=args.output/'fixture')
        positive_requests = list(requests)
        reject[0] = True
        negative_fixture=runtime_case_paths('test_001',ROOT/'test_cases',args.output/'negative_fixture')
        negative = run_case(baseline, ROOT / 'test_cases/test_001/input.md', args.output / 'negative', endpoint,
            timeout=90, working_directory=negative_fixture['repository'],fixture_case_id='test_001',fixture_cases_root=ROOT/'test_cases',fixture_output=args.output/'negative_fixture')
        stdout = (args.output / 'positive/stdout.log').read_text() if (args.output / 'positive/stdout.log').exists() else ''
        checks = {'actual_CLI_made_mock_model_request': bool(positive_requests),
            'request_locked_model': bool(positive_requests) and all(item['model'] == 'gpt-5.6-sol' for item in positive_requests),
            'actual_CLI_terminal_contains_mocked_response': terminal in stdout,
            'positive_transport_preflight': positive.get('transport_preflight', {}).get('valid') is True,
            'baseline_missing_artifact_not_manufactured': not (args.output / 'positive/workspace/agent_result.json').exists(),
            'actual_SDK_endpoint_rejection_is_infrastructure': negative.get('infrastructure_invalid') is True}
        if args.exercise_tool:checks['actual_native_read_file_tool_return_consumed']=any(row.get('tool_return_seen') for row in positive_requests)
        if broker_server:
            checks['real_broker_single_attempts']=bool(broker_state.stats['requests']) and all(r['transport_attempts']==1 for r in broker_state.stats['requests'])
            checks['real_broker_durable_context']=all(row.get('context_id') for row in broker_state.ledger.snapshot().get('logical_requests',{}).values())
        result = {'all_passed': all(checks.values()), 'checks': checks, 'provider_calls': 0,
            'mock_requests': requests, 'positive': positive, 'negative': negative,
            'semantic_score_claimed': False, 'baseline_not_a_repaired_Candidate': True}
        (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'all_passed': result['all_passed'], 'checks': checks, 'mock_requests': requests,
            'positive_classification': positive.get('classification'), 'negative_classification': negative.get('classification')}), flush=True)
        return 0 if result['all_passed'] else 2
    finally:
        if broker_server:broker_server.shutdown();broker_server.server_close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    raise SystemExit(main())
