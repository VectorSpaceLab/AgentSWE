"""One evaluator-owned lower broker with run-local persistent intent."""
from pathlib import Path
import hashlib
import json
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parent
FILES=('lower_responses_broker.py','lower_request_ledger.py','responses_stream.py')


def start_lower_broker(*, name, credential, image, port, cidfile,
                       evaluator_proxy_url='', defer_removal=False):
    # Direct egress by default. Measured on the real lower request shape (71 KB body,
    # ~35k input tokens): direct completed 4/4 in 4.3-6.3 s while the loopback proxy
    # completed 2/4, its failures receiving zero response bytes before timing out --
    # the same signature the Builder relay recorded as 1693 bytes out, 39 bytes back.
    # The container reaches the provider directly, so the extra hop only adds risk.
    credential=Path(credential).resolve();cidfile=Path(cidfile).resolve()
    if cidfile.exists() or cidfile.is_symlink():
        raise FileExistsError('existing Lower CID evidence cannot be replaced')
    cidfile.parent.mkdir(parents=True,exist_ok=True)
    evidence=cidfile.parent/(cidfile.stem+'-lower-transport')
    evidence.mkdir(exist_ok=False)
    refs={str(ROOT/f):hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in FILES}
    (evidence/'source_bindings.json').write_text(json.dumps(refs,indent=2)+'\n')
    mounts=[item for f in FILES for item in ('-v',f'{ROOT/f}:/opt/lower_broker/{f}:ro')]
    command=['docker','run','-d',* ([] if defer_removal else ['--rm']),'--name',name,'--network','host',
             '--label','agentswe.owner=edit-lower-broker-v2',
             '--label',f'agentswe.run_path_sha256={hashlib.sha256(str(cidfile.parent).encode()).hexdigest()}',
             '--cidfile',str(cidfile),'-v',f'{credential}:/run/secrets/agentswe.env:ro',
             '-v',f'{evidence}:/evidence',*mounts,'-v','/etc/ssl/certs:/etc/ssl/certs:ro',
             '-e','SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt',
             '-e','AGENTSWE_EVALUATOR_PROXY_URL='+evaluator_proxy_url,image,
             'python3','/opt/lower_broker/lower_responses_broker.py','--credential-file','/run/secrets/agentswe.env',
             '--stats-file','/evidence/broker_stats.json','--bind','127.0.0.1','--port',str(port)]
    result=subprocess.run(command,capture_output=True,text=True,check=True)
    if not cidfile.is_file():
        value=result.stdout.strip()
        if len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise RuntimeError('Lower startup did not return a valid owned CID')
        cidfile.write_text(value+'\n')
    cid=cidfile.read_text().strip()
    info=json.loads(subprocess.check_output(['docker','inspect',cid]))[0]
    if not any(m.get('Source')==str(evidence) and m.get('Destination')=='/evidence' for m in info['Mounts']):
        raise RuntimeError('Lower broker evidence mount differs from requested run')
    (evidence/'container_identity.json').write_text(json.dumps({'id':info['Id'],'image':info['Image'],
        'labels':info['Config']['Labels'],'mounts':info['Mounts'],'command':info['Config']['Cmd']},indent=2)+'\n')
    for _ in range(60):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=1) as r:
                health=json.load(r)
            if health.get('model')=='deepseek-flash' and health.get('reasoning_effort')=='high' and health.get('protocol')=='agentswe-lower-single-upstream/v1':return
        except (OSError,ValueError):pass
        time.sleep(.25)
    raise RuntimeError('Lower broker did not become healthy; preserve CID and evidence')
