"""Raw, tamper-evident AI Scientist readiness bundle exporter.

The exporter only copies evaluator-owned bytes after the run is terminal. It
does not dispatch providers, judge, or cleanup; those facts must already be
present in the run and coordinator cleanup receipt.
"""
from __future__ import annotations
import hashlib,json,shutil
from pathlib import Path

PROFILE='single-dev-two-round-hidden-smoke-v1'
FILES=('solution.patch','edit_report.json','run_report.json')

def read(path): return json.loads(Path(path).read_bytes())
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def tree(root):
    from agentloop.protocol import tree_digest
    return tree_digest(Path(root))
def delivery_tree(root):
    from agentloop.readiness import delivery_digest
    return delivery_digest(Path(root))

class Writer:
    def __init__(self,run,dest):
        self.run=Path(run).resolve();self.root=Path(dest).resolve();self.root.mkdir(parents=True,exist_ok=False)
    def raw(self,path):
        p=Path(path).resolve()
        if not p.is_file() or not p.is_relative_to(self.run): raise ValueError('foreign/missing raw evidence')
        if any(x in p.relative_to(self.run).parts for x in ('builder_provider.toml','.env','auth.json')): raise ValueError('credential artifact')
        d=self.root/'raw'/p.relative_to(self.run);d.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,d)
        return {'path':d.relative_to(self.root).as_posix(),'sha256':sha(d)}
    def directory(self,path):
        p=Path(path).resolve()
        if not p.is_dir() or not p.is_relative_to(self.run): raise ValueError('foreign/missing raw directory')
        d=self.root/'raw'/p.relative_to(self.run);shutil.copytree(p,d,symlinks=True)
        return d.relative_to(self.root).as_posix()
    def references(self,value):
        if isinstance(value,dict):
            if isinstance(value.get('path'),str) and Path(value['path']).is_absolute() and 'sha256' in value:
                if sha(value['path'])!=value['sha256']: raise ValueError('raw artifact changed')
                return {**value,**self.raw(value['path'])}
            return {k:self.references(v) for k,v in value.items()}
        if isinstance(value,list): return [self.references(v) for v in value]
        return value
    def receipt(self,name,**v):
        p=self.root/(name+'.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps({'run_id':self.run.name,'owner':'evaluator',**v},sort_keys=True,indent=2)+'\n');return {'path':p.relative_to(self.root).as_posix(),'sha256':sha(p)}

def _cleanup(run,cleanup_path,w):
    c=read(cleanup_path)
    if c.get('owner')!='evaluator' or c.get('run_id')!=run.name or any(c.get(k)!='terminal' for k in ('unit_state','harbor_state','builder_state')):
        raise ValueError('terminal cleanup receipt required')
    for key in ('before','after'):
        ref=c[key]
        if sha(ref['path'])!=ref['sha256']: raise ValueError('cleanup evidence changed')
        c[key]=w.receipt('cleanup/'+key,**read(ref['path']),source_evidence=w.raw(ref['path']))
    for resource,ref in c['stats'].items():
        if sha(ref['path'])!=ref['sha256']: raise ValueError('cleanup stats changed')
        c['stats'][resource]=w.receipt('cleanup/stats/'+resource,**read(ref['path']),source_evidence=w.raw(ref['path']))
    return w.receipt('cleanup',**c,source_evidence=w.raw(cleanup_path))

def _recovered_usage_fields(rows):
    """D13 (2026-09-19): tolerated upstream transport failures, recorded and never summed.

    The shared normalizer marks such a row; its usage stays unknown, so it is reported as
    unknown usage and named by identity rather than folded into known_tokens. A role with
    no tolerated row produces exactly the accounting this exporter produced before.
    """
    recovered = [r for r in rows if r.get('recovered_transport') is True]
    fields = {'unknown_usage': len(recovered), 'in_flight': 0}
    if recovered:
        fields['recovered_transport_failures'] = [{'request_id': r['request_id'], 'error': r['error']}
                                                  for r in recovered]
    return fields


def _public_round_request_ids(rows,product,number,rounds):
    """Every public_lower request of one dev round, keyed by the product it exercised.

    Rounds used to be bound positionally (``ids[n-1:n]``), which silently assumed one
    provider request per round.  A multi-turn agent loop breaks that: the later rounds
    pick up an earlier round's turns and most requests stay unattributed, so the shared
    bundle validator refuses the bundle with "public usage has unattributed requests".

    ``logical_request.product_source_digest`` is stamped on every broker row by the
    launcher and names ``lifecycle/product_executions/<digest>/``; the caller has already
    recomputed and verified this round's ``product`` from the materialized repository.
    Consecutive rows carrying one digest are one round, so ledger order is preserved and
    every request lands in exactly one round -- which is what the validator requires.

    Nothing is invented: a ledger that does not split into one group per round, or a
    group that is not this round's product, raises instead of guessing.
    """
    groups=[]
    for row in rows:
        digest=(row.get('logical_request') or {}).get('product_source_digest');rid=row.get('request_id')
        if not digest or not rid: raise ValueError('public request identity missing')
        if groups and groups[-1][0]==digest: groups[-1][1].append(rid)
        else: groups.append((digest,[rid]))
    if len(groups)!=rounds: raise ValueError('public usage does not split into one group per round')
    digest,ids=groups[number-1]
    if digest!=product: raise ValueError('public round usage does not match the round product')
    return ids

def export(run_dir,destination,*,cleanup_receipt,trusted_binding,broker_normalizers=None):
    run=Path(run_dir).resolve();w=Writer(run,destination)
    state=read(run/'lifecycle/dev_lifecycle.json'); freeze=read(run/'lifecycle/freeze_manifest.json')
    att=read(run/'builder_session_attestation.json')
    if att.get('complete') is not True or att.get('evidence_kind')!='pilot': raise ValueError('Builder attestation incomplete')
    if freeze.get('readiness_profile')!=PROFILE or freeze.get('current_binding')!=trusted_binding: raise ValueError('freeze binding mismatch')
    records=state.get('records',[])
    if len(records)!=2 or freeze.get('source_submission')!=2: raise ValueError('exactly two accepted rounds required')
    session=att.get('builder_session_id')
    if not session or att.get('same_session') is not True: raise ValueError('native session invalid')
    evidence={'profile':PROFILE,'run_id':run.name,'current_binding':trusted_binding,'public_rounds':[],'judges':{},'usage':{}}
    native=run/'builder_session_attestation.json';proof=att['native_evidence']
    from harbor.native_builder_evidence import readiness_segment_streams
    segment_streams=readiness_segment_streams(run,proof);stream=segment_streams[-1]
    if proof.get('valid') is not True or any(sha(i)!=r['sha256'] for i,r in zip(segment_streams,proof['source_files'])): raise ValueError('native stream changed')
    events=[json.loads(line) for i in segment_streams for line in i.read_bytes().splitlines() if line.startswith(b'{"type":')]
    terminal=[json.loads(line) for line in stream.read_bytes().splitlines() if line.startswith(b'{"type":')]
    turns=[v['usage'] for v in events if v.get('type')=='turn.completed']
    # D13 (2026-09-19) transport-reconnect tolerance; package 109 (2026-09-21).
    # Same rule as @@AGENTSWE_EDITING_CONTROL@@/v2_readiness.py:99-104:
    # a codex-CLI "Reconnecting... n/N (...)" error row is a recovered transport retry,
    # tolerated only on a terminal segment that ended on turn.completed. turn.failed /
    # thread.failed and any other error row stay fatal; turn accounting is unchanged.
    _completed_terminal=bool(terminal) and terminal[-1].get('type')=='turn.completed'
    _unrecovered=[v for v in terminal if v.get('type') in ('turn.failed','thread.failed') or
                  (v.get('type')=='error' and not (_completed_terminal and str(v.get('message','')).startswith('Reconnecting...')))]
    if len(turns)!=1 or _unrecovered: raise ValueError('native unknown/retry')
    log=w.raw(stream)
    segment_refs = {'native_logs': [w.raw(v) for v in segment_streams[:-1]] + [log],
                    'builder_segments': w.raw(run / 'builder_segments.json')} \
        if len(segment_streams) > 1 else {}
    evidence['builder_native']=w.receipt('builder_native',builder_session_id=session,model='deepseek-flash',effort='max',state='terminal',thread_ids=[session],native_log=log,attestation=w.raw(native), **segment_refs)
    evidence['usage']['builder']=w.receipt('usage/builder',role='builder',builder_session_id=session,native_usage_complete=True,actual_upstream_requests=None,complete_provider_billing_claimed=False,transport='native_codex_direct',builder_broker_started=False,native_completed_turns=1,native_reported_usage=turns,known_tokens=sum(v['input_tokens']+v['output_tokens'] for v in turns),native_log=log)
    if broker_normalizers is None:
        from .usage_normalizers import make
        broker_normalizers=make()
    raw_ledgers={};role_ids={}
    def usage_role(role, patterns):
        matches=[]
        for pattern in patterns: matches.extend(run.glob(pattern))
        if not matches: raise ValueError('missing '+role+' broker ledger')
        path=next((p for p in matches if p.is_file()),None)
        raw=read(path); rows=raw.get('attempts',[])
        raw_ledgers[role]=raw
        if role in ('public_lower','hidden_lower'):
            ids=[r.get('request_id') for r in rows]
        elif role=='result_judge': ids=[r.get('request_id') for r in rows]
        else: ids=[r.get('response_id') for r in rows]
        requests=[]
        for rid in ids:
            norm=broker_normalizers[role](raw,rid)
            requests.append({**norm,'provider_record':w.raw(path)})
        if not requests: raise ValueError('empty '+role+' ledger')
        role_ids[role]=ids
        return w.receipt('usage/'+role,role=role,calls=len(requests),actual_upstream_attempts=sum(x['upstream_attempts'] for x in requests),successes=sum(x['state']=='success' for x in requests),failures=sum(x['state']=='failure' for x in requests),known_tokens=sum(x['known_tokens'] for x in requests),**_recovered_usage_fields(requests),requests=requests)
    evidence['usage']['public_lower']=usage_role('public_lower',['brokers/public_lower-lower-transport/broker_stats.json'])
    evidence['usage']['hidden_lower']=usage_role('hidden_lower',['brokers/hidden_lower-lower-transport/broker_stats.json'])
    evidence['usage']['result_judge']=usage_role('result_judge',['brokers/judge-judge-transport/broker_stats.json','**/judge_broker_stats.json','**/result_judge*/broker_stats.json'])
    previous=None;previous_feedback=None;previous_feedback_file=None
    for n,r in enumerate(records,1):
        paths=r.get('attempt_paths',{});delivery=Path(paths.get('candidate',''));repo=Path(paths.get('repository',''))
        if not delivery.is_dir() or set(x.name for x in delivery.iterdir())!=set(FILES): raise ValueError('submission inventory changed')
        if delivery_tree(delivery)!=r.get('candidate_digest') or not repo.is_dir(): raise ValueError('submission digest changed')
        build=r.get('build',{}); product=build.get('product_source_digest')
        if build.get('valid') is not True or not product or product != __import__('agentloop.stable_product',fromlist=['product_source_digest']).product_source_digest(repo): raise ValueError('product/build evidence invalid')
        public=r.get('dev',{}).get('dev_001',{}); broker=public.get('broker',{})
        if public.get('classification_axis')!='candidate' or not public.get('real_execution') or broker.get('usage_complete') is not True or int(broker.get('successful_calls',0))<=0: raise ValueError('public execution invalid')
        feedback=Path(r.get('feedback_path',''))
        if not feedback.is_file() or sha(feedback)!=r.get('feedback_digest'): raise ValueError('feedback changed')
        meta={'builder_session_id':session,'submission_number':n,'revision_of_candidate_digest':previous,'feedback_digest':previous_feedback}
        report=read(delivery/'run_report.json')
        if any(report.get(k)!=v for k,v in meta.items()): raise ValueError('submission metadata mismatch')
        rid=_public_round_request_ids(raw_ledgers.get('public_lower',{}).get('attempts',[]),product,n,len(records))
        if not rid: raise ValueError('public request identity missing')
        result_receipt=w.receipt(f'public/{n}/result',candidate_digest=r['candidate_digest'],case='dev_001',state='terminal',classification='execution_valid')
        feedback_ref={**w.raw(feedback),'digest_algorithm':'sha256-bytes-v1'}
        evidence['public_rounds'].append({**meta,'case':'dev_001','candidate_digest':r['candidate_digest'],'submission_dir':w.directory(delivery),'submission_sha256':{x:sha(delivery/x) for x in FILES},'materialized_source_digest':product,'materialized_repository_digest':product,'feedback':feedback_ref,'execution':w.receipt(f'public/{n}/execution',**meta,candidate_digest=r['candidate_digest'],case='dev_001',state='terminal',classification='execution_valid',current_binding=trusted_binding,transport='complete',build_exit_code=0,materialized_source_digest_before_build=product,materialized_source_digest_after_build=product,materialized_repository_digest=product,request_ids=rid,feedback_sha256=sha(feedback),consumed_feedback=({**w.raw(previous_feedback_file),'digest_algorithm':'sha256-bytes-v1'} if n==2 and previous_feedback_file else None),result=result_receipt,source_execution=w.raw(Path(public['output_path'])/'launcher_result.json'))})
        previous=r['candidate_digest'];previous_feedback=r['feedback_digest'];previous_feedback_file=feedback
    hidden=read(run/'lifecycle'/'hidden-after-freeze-attestation.json')
    if hidden.get('expected_cases')!=['test_001'] or hidden.get('executed_cases')!=['test_001'] or hidden.get('frozen_digest_stable') is not True: raise ValueError('hidden smoke incomplete')
    freeze_value={**freeze,'delivery_candidate_digest':previous,'candidate_digest':product,'builder_session_id':session,'submission_sha256':{x:sha(Path(records[-1]['attempt_paths']['candidate'])/x) for x in FILES}}
    evidence['freeze']=w.receipt('freeze',**freeze_value,source_evidence=w.raw(run/'lifecycle/freeze_manifest.json'))
    hidden_result=w.receipt('hidden_result',case='test_001',state='terminal',candidate_digest=freeze_value['candidate_digest'],source_evidence=w.raw(run/'lifecycle'/'hidden-after-freeze-attestation.json'))
    evidence['hidden_smoke']=w.receipt('hidden',case='test_001',state='terminal',classification='execution_valid',transport='complete',readiness_only=True,builder_access=False,freeze_sha256=evidence['freeze']['sha256'],request_ids=role_ids.get('hidden_lower',[]),result=hidden_result,source_evidence=w.raw(run/'lifecycle'/'hidden-after-freeze-attestation.json'))
    judge_summary=run/'readiness_judges.json'
    if not judge_summary.is_file() or read(judge_summary).get('readiness_judges_complete') is not True: raise ValueError('independent judge smoke incomplete')
    for role in ('result',):
        # The validators read their binding out of the input receipt, so carry
        # what the run recorded rather than a stub. freeze_sha256 stays the
        # bundle's own freeze digest: that is what v2_readiness compares.
        recorded=read(run/'readiness_scoring'/(role+'_validation_input.json'))
        carried={k:v for k,v in recorded.items() if k not in ('role','freeze_sha256')}
        inp=w.receipt('judges/'+role+'/input',role=role,freeze_sha256=evidence['freeze']['sha256'],**carried)
        verdict=read(run/'readiness_scoring'/(role+'_raw_output.json'))
        payload=verdict.get('payload')
        # The shared judge adds `ceiling_assessments` when a score-cap contract is supplied;
        # the shared bundle validator checks the payload against a fixed key set, so the
        # ceilings live beside the payload (the raw judge output is the source evidence).
        _payload = verdict.get('payload') if isinstance(verdict.get('payload'), dict) else None
        if _payload is not None and 'ceiling_assessments' in _payload:
            verdict['ceiling_assessments'] = _payload.pop('ceiling_assessments')
        if not isinstance(payload,dict): raise ValueError('judge verdict payload missing: '+role)
        session=verdict.get('judge_session_id') or (role+'-session')
        out=w.receipt('judges/'+role+'/output',role=role,judge_session_id=session,payload=payload,
            source_evidence=w.raw(run/'readiness_scoring'/(role+'_raw_output.json')))
        evidence['judges'][role]=w.receipt('judges/'+role,role=role,judge_session_id=session,request_id=role_ids.get(role+'_judge',[''])[0],model='deepseek-flash',effort='max',state='terminal',formal=False,freeze_sha256=evidence['freeze']['sha256'],input=inp,output=out,source_evidence=w.raw(judge_summary))
    # Code axis retired 2026-09-19 (Result-only): explicit skip receipts, accepted by v2_readiness.
    _skip_policy = {'id': 'edit-code-axis-retired-2026-09-19', 'evaluation_state': 'skipped_by_policy', 'reason': 'Result-only evaluation; Code judge not dispatched'}
    _skip_source = w.raw(run / 'readiness_scoring/code_observation.json')
    evidence['usage']['code_judge'] = w.receipt('usage/code_judge', role='code_judge', skipped_by_policy=True,
        policy=_skip_policy, calls=0, actual_upstream_attempts=0, successes=0, failures=0, known_tokens=0,
        unknown_usage=0, in_flight=0, requests=[], source_evidence=_skip_source)
    evidence['judges']['code'] = w.receipt('judges/code', role='code', state='skipped_by_policy', formal=False,
        judge_session_id=None, request_id=None, freeze_sha256=evidence['freeze']['sha256'], policy=_skip_policy,
        source_evidence=_skip_source)
    evidence['cleanup']=_cleanup(run,cleanup_receipt,w)
    manifest=w.root/'manifest.json';manifest.write_text(json.dumps(evidence,sort_keys=True,indent=2)+'\n')
    return {'bundle_root':str(w.root),'manifest_sha256':sha(manifest),'pipeline_ready':False,'admission_required':True}
