"""Verifier-owned browser evidence and identity binding for data/web tasks."""
from __future__ import annotations
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def tree_digest(root):
    h=hashlib.sha256()
    for path in sorted(root.rglob('*'),key=lambda p:p.relative_to(root).as_posix()):
        rel=path.relative_to(root).as_posix()
        if path.is_symlink():raise ValueError('unsafe input link')
        if path.is_file():
            h.update(rel.encode());h.update(b'\0');h.update(path.read_bytes());h.update(b'\0')
    return h.hexdigest()


def browser(files,entry,steps,work,disable_javascript=False):
    plan={'files':{name:{'base64':base64.b64encode(path.read_bytes()).decode(),'mime':mimetypes.guess_type(name)[0] or 'text/plain'} for name,path in files.items()},'entry':entry,'steps':steps,'disable_javascript':disable_javascript}
    work.mkdir(parents=True,exist_ok=True)
    plan_path=work/'browser_plan.json';plan_path.write_text(json.dumps(plan))
    env={key:value for key,value in os.environ.items() if not any(token in key.upper() for token in ('KEY','TOKEN','SECRET','AUTH','PROXY'))}
    try:
        cp=subprocess.run([os.environ.get('NODE','node'),str(Path(__file__).with_name('browser_probe.mjs')),str(plan_path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=150,env=env)
        (work/'browser.stderr.log').write_text(cp.stderr)
        result=json.loads(cp.stdout)
        if cp.returncode:raise RuntimeError('browser worker exited '+str(cp.returncode))
    except Exception as exc:
        result={'valid':False,'evaluation_state':'infrastructure_error','errors':[type(exc).__name__+': '+str(exc)],'snapshots':[]}
    (work/'browser_evidence.json').write_text(json.dumps(result))
    return result


def dashboard(output,independent,work):
    if not (output/'dashboard.html').is_file():return {'valid':False,'evaluation_state':'scoreable','errors':['dashboard missing'],'snapshots':[]}
    result=browser({'dashboard.html':output/'dashboard.html'},'dashboard.html',[{'action':'inspect_controls'}],work)
    errors=result.setdefault('errors',[])
    if result.get('evaluation_state')=='infrastructure_error':return result
    initial=result['snapshots'][0]['observed'] if result.get('snapshots') else {}
    selectors={'dev_001':('market-select','market'),'dev_002':('plan-select','plan'),'test_001':('country-select','seller_country'),'test_002':('division-select','division'),'test_003':('site-select','site'),'test_004':('carrier-service-select','carrier'),'test_006':('warehouse-select','warehouse')}
    cid=independent['case_id']
    if cid in selectors:
        control,field=selectors[cid];records=independent['expected_rows'];values=sorted({str(r[field]) for r in records});snapshots=[s for s in result['snapshots'] if s['step'].get('target')==control]
        for value in values:
            matches=[s for s in snapshots if value in (str(s['step'].get('value','')),str(s['step'].get('text','')))]
            if not matches:errors.append({'control':control,'missing_option':value});continue
            text=matches[0]['observed']['text'];want=[r for r in records if str(r[field])==value]
            if not all(any(str(v) in text for v in r.values() if isinstance(v,(int,float)) and not isinstance(v,bool)) for r in want):errors.append({'control':control,'value':value,'error':'selected result values not visible'})
        if len(values)>1 and len({json.dumps(s['observed']['rows']) for s in snapshots})<2:errors.append({'control':control,'error':'changing selector did not change visible result rows'})
    interactions=[s for s in result.get('snapshots',[]) if s['step']['action']!='open']
    if not interactions:errors.append('no usable dashboard controls')
    result['valid']=not errors
    result['assertions_scope']='real rendered controls/rows; full screenshots supplied for rubric judgment'
    return result


def load_contract(manifest,case_dir):
    execution=manifest.get('candidate_execution_contract',{})
    value=execution.get('trusted_harness_result') if isinstance(execution,dict) else None
    if not isinstance(value,dict) or value.get('case')!=case_dir.name:raise RuntimeError('trusted evidence missing or case mismatch')
    if digest(value)!=execution.get('trusted_harness_result_sha256'):raise RuntimeError('trusted evidence digest mismatch')
    for a,b in (('candidate_digest','candidate_digest'),('output_digest','candidate_output_digest'),('case_digest','case_digest')):
        if not isinstance(manifest.get(b),str) or len(manifest[b])!=64 or execution.get(a)!=manifest[b]:raise RuntimeError('trusted identity mismatch: '+a)
    if value.get('evaluation_state')=='infrastructure_error':raise RuntimeError('trusted verifier infrastructure failure')
    return value


def images(value):
    found=[]
    def walk(node):
        if isinstance(node,dict):
            if node.get('mime')=='image/png' and isinstance(node.get('base64'),str):
                raw=base64.b64decode(node['base64'],validate=True)
                if hashlib.sha256(raw).hexdigest()!=node.get('sha256') or not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('trusted image hash mismatch')
                found.append({'type':'input_image','image_url':'data:image/png;base64,'+node['base64']})
            else:
                for child in node.values():walk(child)
        elif isinstance(node,list):
            for child in node:walk(child)
    walk(value)
    return found


def text_evidence(value):
    if isinstance(value,dict):return {k:text_evidence(v) for k,v in value.items() if k!='base64'}
    if isinstance(value,list):return [text_evidence(v) for v in value]
    return value
