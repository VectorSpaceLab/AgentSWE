"""Real isolated Node fetch -> localhost bridge -> UDS -> mock broker; no API."""
from __future__ import annotations
import argparse
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.transport_sandbox import FixedLowerRelay


def sandbox(command, output, relay, node):
    argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
        '--unshare-ipc', '--unshare-uts', '--unshare-net',
        '--ro-bind', '/usr', '/usr', '--ro-bind', '/bin', '/bin',
        '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
        '--dir', '/run/agentswe', '--ro-bind', str(relay.socket_path), '/run/agentswe/lower.sock',
        '--ro-bind', str(ROOT / 'agentloop/evaluator/transport_sandbox.py'), '/run/agentswe/transport.py',
        '--bind', str(output), str(output), '--ro-bind', str(node.parent.parent), str(node.parent.parent),
        '--chdir', str(output), '/usr/bin/python3', '/run/agentswe/transport.py', '--inside',
        '--uds', '/run/agentswe/lower.sock', '--preflight', str(output / 'transport_preflight.json'), '--', *command]
    return subprocess.run(argv, env={'PATH': '/usr/bin:/bin', 'HOME': str(output)},
        capture_output=True, text=True, timeout=45)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    requests = []
    class Mock(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'path': self.path, 'body': body})
            raw = b'{"output_text":"mock-only"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = FixedLowerRelay(f'http://127.0.0.1:{server.server_port}/v1/responses').start()
    node = ROOT / '.runtime/node-v22.12.0-linux-x64/bin/node'
    code = r'''(async()=>{
const response=await fetch(process.env.OPENAI_BASE_URL+'/responses', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:'gpt-5.6-sol',reasoning:{effort:'high'},input:'mock-only'})});
const result=await response.json(); let hostTcpBlocked=false;
try { await fetch('http://127.0.0.1:HOST_PORT/healthz', {signal:AbortSignal.timeout(1000)}); } catch(e) {hostTcpBlocked=true;}
const denied=await fetch(process.env.OPENAI_BASE_URL+'/arbitrary-host', {method:'POST',body:'{}'});
process.stdout.write(JSON.stringify({result,hostTcpBlocked,unmappedStatus:denied.status}));
})().catch(e=>{console.error(e);process.exit(2)})'''.replace('HOST_PORT', str(server.server_port))
    try:
        process = sandbox([str(node), '-e', code], output, relay, node)
        (output / 'stdout.log').write_text(process.stdout)
        (output / 'stderr.log').write_text(process.stderr)
        actual = json.loads(process.stdout) if process.returncode == 0 else {}
        preflight = json.loads((output / 'transport_preflight.json').read_text())
        checks = {'actual_Node_fetch_through_fixed_relay': actual.get('result') == {'output_text': 'mock-only'},
            'host_TCP_blocked': actual.get('hostTcpBlocked') is True,
            'unmapped_path_blocked': actual.get('unmappedStatus') == 404,
            'only_one_mock_request': len(requests) == 1,
            'locked_model_request_preserved': requests and requests[0]['body'].get('model') == 'gpt-5.6-sol'
                and requests[0]['body'].get('reasoning') == {'effort': 'high'},
            'inside_preflight_valid': preflight.get('valid') is True}
        result = {'checks': checks, 'passed': all(checks.values()), 'provider_calls': 0,
            'mock_requests': requests, 'relay_events': relay.events, 'relay_errors': relay.errors,
            'exit_code': process.returncode}
        (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        return 0 if result['passed'] else 1
    finally:
        relay.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
