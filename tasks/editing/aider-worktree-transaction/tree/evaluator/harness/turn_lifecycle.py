"""Stop the exact active lower CLI when its fixed transport becomes invalid."""
from __future__ import annotations
import json,re,subprocess,time
from pathlib import Path


def stop_owned_turn(command, cidfile: Path) -> dict:
    """Only the Docker-created private CID and exact configured name may stop."""
    expected_name=command[command.index('--name')+1]
    identity=cidfile.read_text().strip() if cidfile.is_file() else ''
    if not re.fullmatch('[0-9a-f]{64}',identity):
        raise RuntimeError('cannot stop failed transport without an exact owned lower CID')
    inspected=subprocess.run(['docker','inspect',identity],text=True,capture_output=True,timeout=5,check=False)
    if inspected.returncode:
        absent=any(text in inspected.stderr.lower() for text in ('no such object','no such container'))
        if not absent:raise RuntimeError('lower CID inspection failed')
        return {'container_id':identity,'already_absent':True,'identity_source':'private Docker cidfile'}
    value=json.loads(inspected.stdout)[0]
    if value.get('Id')!=identity or value.get('Name')!='/'+expected_name:
        raise RuntimeError('lower CID/name mismatch; refusing unrelated process control')
    done=subprocess.run(['docker','kill',identity],text=True,capture_output=True,timeout=5,check=False)
    after=subprocess.run(['docker','inspect',identity],text=True,capture_output=True,timeout=5,check=False)
    if after.returncode:
        stopped=any(text in after.stderr.lower() for text in ('no such object','no such container'))
    else:
        state=json.loads(after.stdout)[0]
        stopped=state.get('Id')==identity and state.get('State',{}).get('Running') is False
    if not stopped:raise RuntimeError('failed-transport lower container did not stop')
    return {'container_id':identity,'expected_name':expected_name,'identity_verified':True,'kill_exit':done.returncode,'stopped':True}


def run_turn(command, *, input_text: str, output: Path, timeout: float, transport_errors):
    """Current turn finishes or stops; SDK backoff cannot create later turns."""
    output.mkdir(parents=True,exist_ok=False)
    cidfile=Path(command[command.index('--cidfile')+1])
    receipt={'schema_version':'agentswe-aider-turn-transport-boundary/v1','transport_failed':False,
             'continuation_allowed':False,'candidate_failure_inferred':False,'stop':None}
    started=time.monotonic();deadline=started+timeout;timed_out=False
    with (output/'stdout.log').open('x') as stdout,(output/'stderr.log').open('x') as stderr:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,text=True)
        try:
            try:process.stdin.write(input_text);process.stdin.close()
            except BrokenPipeError:pass
            while True:
                failures=transport_errors()
                if failures:
                    receipt.update(transport_failed=True,observed_transport_errors=failures,
                                   reason='first fixed relay/provider failure; abort active CLI and forbid continuation')
                    receipt['stop']=stop_owned_turn(command,cidfile)
                    break
                if process.poll() is not None:break
                if time.monotonic()>=deadline:
                    timed_out=True;receipt['stop']=stop_owned_turn(command,cidfile);break
                time.sleep(.05)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        finally:
            if process.poll() is None:
                receipt['stop']=stop_owned_turn(command,cidfile)
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            receipt.update(process_exit=process.returncode,elapsed_seconds=time.monotonic()-started,timed_out=timed_out)
            receipt['continuation_allowed']=not receipt['transport_failed'] and not timed_out and process.returncode==0
            (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    stdout=(output/'stdout.log').read_text();stderr=(output/'stderr.log').read_text()
    if timed_out:raise subprocess.TimeoutExpired(command,timeout,output=stdout,stderr=stderr)
    return subprocess.CompletedProcess(command,process.returncode,stdout,stderr),receipt
