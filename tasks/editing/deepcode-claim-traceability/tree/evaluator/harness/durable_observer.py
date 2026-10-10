"""Evaluator-only black-box faults against real Candidate CLI and durable disk.

Runs after the lower agent, against a disposable snapshot, without a broker.
Every process sees only the Candidate, observed workspace and runtime. Expected
invariants and these observer instructions are never mounted into that process.
A missing lower-produced capsule is evidence, never filled by this observer.
"""
from __future__ import annotations
import concurrent.futures,hashlib,json,os,secrets,shutil,subprocess,time
from pathlib import Path
from deepcode_lower_agent import sandbox_command,candidate_environment

FILES=('paper_spec.json','traceability_graph.json','reproduction_manifest.json','assumptions.json','deviations.json','data_manifest.json','checksums.json','environment.lock','replay.py')
def read(path):
    return json.loads(path.read_text())
def checksum_tree(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}
def observe_durable(case_id, *, repository, workspace, python_executable, output, deadline):
    output.mkdir(parents=True,exist_ok=False)
    result={'schema_version':'deepcode-durable-observer/v1','case_id':case_id,'candidate_visible':False,
        'actual_product_cli':True,'provider_calls':0,'source':'snapshot of lower-produced workspace',
        'checks':{},'operations':[],'limitations':[]}
    def finish():
        result['finished_at_monotonic']=time.monotonic()
        (output/'observation.json').write_text(json.dumps(result,indent=2)+'\n');return result
    if not workspace.is_dir():
        result['status']='lower_workspace_missing';return finish()
    caps=[p.parent for p in sorted(workspace.rglob('reproduction_manifest.json')) if not p.is_symlink() and all((p.parent/name).is_file() and not (p.parent/name).is_symlink() for name in FILES)]
    if not caps:
        result['status']='lower_produced_capsule_missing';return finish()
    # Preserve all actual app disk state, but run adversarial operations only in
    # this disposable observer copy. Never manufacture a scientifically valid capsule.
    work=output/'workspace';shutil.copytree(workspace,work,symlinks=True)
    cap=work/caps[0].relative_to(workspace)
    result['lower_capsule_relative_path']=str(cap.relative_to(work))
    home=output/'home';home.mkdir();runtime=output/'runtime';runtime.mkdir()
    operations=work/'.deepcode'/'operations';operations.mkdir(parents=True,exist_ok=True)
    private_nonce=secrets.token_hex(12)
    run_cfg=read(work/'.deepcode/durable_run.json')
    base={'schema_version':'1.0','tenant_id':run_cfg['tenant_id'],'project_id':'observer-'+private_nonce,
        'actor_id':'operator-local','idempotency_key':'observer-'+private_nonce,
        'capsule_path':os.path.relpath(cap,operations),'output_path':'../observer_publication'}
    store='/workspace/.deepcode/observer_runs'
    clean,_=candidate_environment({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    clean.update(HOME='/deepcode-home',DEEPCODE_HOME='/deepcode-home',PYTHONPATH='/candidate')
    def call(action,fields=None,operation_id=None):
        op={**base,'action':action,'operation_id':operation_id or 'obs-'+secrets.token_hex(10),**(fields or {})}
        op_path=operations/(secrets.token_hex(12)+'.json');op_path.write_text(json.dumps(op))
        cmd=[str(python_executable),'-m','workflows.traceability_runs','--store',store,'--operation',str(op_path)]
        cmd,meta=sandbox_command(command=cmd,repository=repository,workspace=work,home=home,isolated=runtime,python_executable=python_executable)
        started=time.monotonic()
        try:
            p=subprocess.run(cmd,env=clean,text=True,capture_output=True,timeout=max(.001,min(45,deadline-started)),check=False)
            try:response=json.loads(p.stdout)
            except ValueError:response={'invalid_json':True}
            record={'operation':op,'exit_code':p.returncode,'response':response,'stdout':p.stdout[-8000:],'stderr':p.stderr[-2000:],
                'elapsed_seconds':time.monotonic()-started,'new_process':True,'sandbox':meta}
        except subprocess.TimeoutExpired:
            record={'operation':op,'exit_code':124,'response':{},'timed_out':True,'new_process':True}
        result['operations'].append(record)
        return record
    submit_id='obs-submit-'+private_nonce
    if case_id=='test_001':
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            submissions=list(pool.map(lambda _:call('submit',operation_id=submit_id),range(8)))
        accepted=[r for r in submissions if r['response'].get('accepted') is True]
        result['checks']['eight_real_concurrent_submit_receipts']=len(accepted)==8 and all(r['response']==accepted[0]['response'] for r in accepted)
        result['response_loss_simulation']='first response withheld from caller; remaining seven are exact retries from independent processes'
        submitted=submissions[-1]
        changed=call('submit',{'output_path':'../changed-observer-publication'},submit_id)
        result['checks']['changed_body_conflicts']=changed['exit_code']==2 and changed['response'].get('error',{}).get('code')=='OPERATION_CONFLICT'
    else:
        submitted=call('submit',operation_id=submit_id)
    result['checks']['real_submit_accepted']=submitted['response'].get('accepted') is True
    record=submitted['response'].get('record') or {}
    run_id=record.get('run_id')
    if not run_id:
        result['status']='candidate_submit_rejected_or_missing_run_id';return finish()
    owner='observer-owner-'+private_nonce
    claim=call('claim',{'run_id':run_id,'owner_id':owner,'worker_id':owner,'logical_time':100,'lease_seconds':1})
    c=claim['response'].get('record') or {}
    current={'run_id':run_id,'owner_id':owner,'worker_id':owner,'generation':c.get('generation'),
        'claim_token':c.get('claim_token'),'logical_time':100}
    result['checks']['generation_is_product_observed']=type(c.get('generation')) is int
    if case_id=='test_002':
        # Truncated staging is actual disk corruption, not an expected-state JSON.
        stage=work/'.deepcode/interrupted_stage';stage.mkdir(exist_ok=True)
        (stage/'paper_spec.json').write_text('{"schema_version":')
        marker=work/'.deepcode/unsafe-stage-executed'
        (stage/'replay.py').write_text("from pathlib import Path;Path('/workspace/.deepcode/unsafe-stage-executed').write_text('bad')\n")
        takeover=call('claim',{'run_id':run_id,'owner_id':owner+'-new','worker_id':owner+'-new','logical_time':10000,'lease_seconds':1})
        newer=takeover['response'].get('record') or {}
        result['checks']['new_process_takeover_rotates_generation']=takeover['response'].get('accepted') is True and type(newer.get('generation')) is int and type(c.get('generation')) is int and newer['generation']>c['generation']
        stale=[call(action,{**current,'logical_time':10001,'capsule_path':os.path.relpath(stage,operations)}) for action in ('heartbeat','fail','commit')]
        result['checks']['stale_owner_rejected']=all(r['exit_code']==2 and r['response'].get('accepted') is False for r in stale)
        result['checks']['partial_stage_not_executed']=not marker.exists()
        result['limitations'].append('Correct retryable recovery after rejected corrupt replay still requires lower-agent trajectory evidence.')
    elif case_id=='test_003':
        foreign=call('get',{'run_id':run_id,'tenant_id':'foreign-'+private_nonce})
        result['checks']['foreign_tenant_hidden']=foreign['exit_code']==2 and foreign['response'].get('accepted') is False
        escaping=call('commit',{**current,'output_path':'../../../../escape-observer-publication'})
        result['checks']['escaping_output_rejected']=escaping['exit_code']==2 and escaping['response'].get('accepted') is False
    commit_id='obs-commit-'+private_nonce
    if case_id=='test_005':
        stale=call('commit',{**current,'generation':-1})
        result['checks']['stale_generation_rejected']=stale['exit_code']==2 and stale['response'].get('accepted') is False
    if case_id in ('test_001','test_003','test_004','test_005','test_006'):
        commit=call('commit',current,commit_id)
        retry=call('commit',current,commit_id)
        result['checks']['terminal_receipt_retry_stable']=commit['response']==retry['response'] and commit['exit_code']==retry['exit_code']
        publication=work/'.deepcode/observer_publication'
        result['publication_digest_before_fault']=checksum_tree(publication) if publication.is_dir() else {}
        if case_id in ('test_004','test_006') and publication.is_dir():
            marker=work/'.deepcode/corrupt-replay-executed'
            (publication/'replay.py').write_text("from pathlib import Path;Path('/workspace/.deepcode/corrupt-replay-executed').write_text('bad')\n")
            first=call('reconcile',{'run_id':run_id});second=call('reconcile',{'run_id':run_id})
            result['checks']['reconcile_does_not_execute_corrupt_replay']=not marker.exists()
            result['reconcile_receipts']=[first['response'],second['response']]
            terminal=call('get',{'run_id':run_id})
            result['terminal_after_corruption']=terminal['response']
            result['checks']['corruption_detected']=any(word in json.dumps(terminal['response']).lower() for word in ('corrupt','invalid','quarantine','blocked'))
        if case_id=='test_006':
            manifest=read(cap/'reproduction_manifest.json')
            result['checks']['lower_capsule_remains_blocked']=manifest.get('status')=='blocked'
        if case_id=='test_005':
            result['limitations'].append('Concurrent publication reader atomicity remains a separate required observation.')
    result['status']='observed';result['all_observed_checks_pass']=all(result['checks'].values())
    return finish()
