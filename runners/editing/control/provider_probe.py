#!/usr/bin/env python3
"""One real evaluator-broker availability request; never a benchmark result."""
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path('@@AGENTSWE_EDITING_CONTROL@@')
sys.path.insert(0, str(ROOT))
from judge_broker_runtime import JudgeBroker, stats

run = Path(sys.argv[1]).resolve()
run.mkdir(parents=True, exist_ok=False)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
broker = JudgeBroker(name='edit-0913-health-' + str(port),
    credential='@@AGENTSWE_CREDENTIAL_FILE@@',
    image='agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812',
    port=port, cidfile=run/'broker.cid')
receipt = {'started_at':datetime.now(timezone.utc).isoformat(),
    'scope':'availability only; not smoke/readiness evidence',
    'credential_values_recorded':False, 'logical_requests':0}
try:
    broker.start()
    request = urllib.request.Request(broker.endpoint, method='POST',
        headers={'Authorization':'Bearer judge-only-placeholder','Content-Type':'application/json',
                 'X-AgentSWE-Judge-Deadline-Seconds':'90'},
        data=json.dumps({'model':'deepseek-flash','reasoning':{'effort':'max'},
            'max_output_tokens':64000,'stream':True,
            'input':[{'role':'user','content':'Reply with exactly OK.'}]}).encode())
    receipt['logical_requests']=1
    try:
        response=urllib.request.urlopen(request,timeout=100)
    except urllib.error.HTTPError as exc:
        response=exc
    with response:
        receipt['http_status']=response.code
        raw=response.read()
    (run/'response.json').write_bytes(raw)
    try:
        result=json.loads(raw)
        receipt.update(response_status=result.get('status'),model=result.get('model'),
                       usage=result.get('usage'),error=result.get('error'))
    except ValueError:
        receipt['parse_error']=True
    receipt['broker_stats']=stats(broker.endpoint)
except Exception as exc:
    receipt['error_type']=type(exc).__name__
finally:
    receipt['cleanup']=broker.close()
    receipt['finished_at']=datetime.now(timezone.utc).isoformat()
    (run/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2),flush=True)
