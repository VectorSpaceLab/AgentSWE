"""One evaluator-owned upper-Builder broker with run-local persistent intent."""
from pathlib import Path
import hashlib
import json
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parent
FILES=('builder_broker_xhigh.py','builder_request_ledger.py','responses_stream.py')


def start_builder_broker(*, name, credential, image, port, cidfile):
    credential=Path(credential).resolve();cidfile=Path(cidfile).resolve()
    if cidfile.exists() or cidfile.is_symlink():
        raise FileExistsError('existing Builder CID evidence cannot be replaced')
    cidfile.parent.mkdir(parents=True,exist_ok=True)
    evidence=cidfile.parent/(cidfile.stem+'-builder-transport')
    evidence.mkdir(exist_ok=False)
    refs={str(ROOT/f):hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in FILES}
    (evidence/'source_bindings.json').write_text(json.dumps(refs,indent=2)+'\n')
    mounts=[item for f in FILES for item in ('-v',f'{ROOT/f}:/opt/builder_broker/{f}:ro')]
    command=['docker','run','-d','--rm','--name',name,'--network','host',
             '--label','agentswe.owner=edit-builder-broker-v2',
             '--label',f'agentswe.run_path_sha256={hashlib.sha256(str(cidfile.parent).encode()).hexdigest()}',
             '--cidfile',str(cidfile),'-v',f'{credential}:/run/secrets/agentswe.env:ro',
             '-v',f'{evidence}:/evidence',*mounts,'-v','/etc/ssl/certs:/etc/ssl/certs:ro',
             '-e','SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt',image,
             'python3','/opt/builder_broker/builder_broker_xhigh.py','--credential-file','/run/secrets/agentswe.env',
             '--stats-file','/evidence/broker_stats.json','--bind','0.0.0.0','--port',str(port)]
    result=subprocess.run(command,capture_output=True,text=True,check=True)
    if not cidfile.is_file():
        value=result.stdout.strip()
        if len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise RuntimeError('Builder startup did not return a valid owned CID')
        cidfile.write_text(value+'\n')
    cid=cidfile.read_text().strip()
    info=json.loads(subprocess.check_output(['docker','inspect',cid]))[0]
    if not any(m.get('Source')==str(evidence) and m.get('Destination')=='/evidence' for m in info['Mounts']):
        raise RuntimeError('Builder broker evidence mount differs from requested run')
    (evidence/'container_identity.json').write_text(json.dumps({'id':info['Id'],'image':info['Image'],
        'labels':info['Config']['Labels'],'mounts':info['Mounts'],'command':info['Config']['Cmd']},indent=2)+'\n')
    for _ in range(60):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=1) as r:
                health=json.load(r)
            if health.get('model')=='deepseek-flash' and health.get('reasoning_effort')=='max' and health.get('protocol')=='agentswe-builder-single-upstream/v1':return
        except (OSError,ValueError):pass
        time.sleep(.25)
    raise RuntimeError('Builder broker did not become healthy; preserve CID and evidence')
