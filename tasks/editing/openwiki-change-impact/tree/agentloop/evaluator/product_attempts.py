"""Evaluator-private one-public-execution ledger per materialized source tree."""
from __future__ import annotations
import hashlib,json,os,re
from pathlib import Path

class ProductReplayBlocked(RuntimeError):
    pass

def encoded(value):
    return (json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2)+'\n').encode()

def durable_json(path: Path,value):
    data=encoded(value)
    with path.open('xb') as handle:
        handle.write(data);handle.flush();os.fsync(handle.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def sealed_json(path: Path,value):
    durable_json(path,value)
    durable_json(path.with_suffix(path.suffix+'.sha256.json'),{'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})

def read_sealed(path: Path):
    seal=path.with_suffix(path.suffix+'.sha256.json')
    if path.is_symlink() or seal.is_symlink():raise ValueError('product receipt symlink')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=json.loads(seal.read_text())['sha256']:
        raise ValueError('product receipt digest changed')
    return json.loads(data)

class ProductAttempts:
    def __init__(self,root: Path):
        self.root=root
        if any(p.is_symlink() for p in (root,*root.parents)):raise ValueError('product ledger path symlink')
        root.mkdir(parents=True,exist_ok=True)

    def directory(self,digest):
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('invalid product source digest')
        return self.root/digest

    def lookup(self,digest):
        directory=self.directory(digest)
        if not os.path.lexists(directory):return None
        value={'state':'unknown_or_in_progress','product_source_digest':digest,
               'case_results':{},'record':None,'retry_allowed':False}
        if directory.is_symlink() or not directory.is_dir():
            return {**value,'state':'unknown_or_invalid_evidence'}
        try:
            intent=read_sealed(directory/'intent.json')
            if intent['product_source_digest']!=digest:raise ValueError('intent identity mismatch')
            for path in (directory/'case_dev_001.json',directory/'case_dev_002.json'):
                if not os.path.lexists(path):continue
                saved=read_sealed(path)
                if saved['product_source_digest']!=digest:raise ValueError('case identity mismatch')
                evidence=Path(saved['evidence_root'])
                for ref in saved['source_files']:
                    relative=Path(ref['relative_path'])
                    if relative.is_absolute() or '..' in relative.parts:raise ValueError('invalid case source reference')
                    p=evidence/relative
                    if not p.is_relative_to(evidence) or any(v.is_symlink() for v in (p,*p.parents)):
                        raise ValueError('case source path escaped evidence')
                    if hashlib.sha256(p.read_bytes()).hexdigest()!=ref['sha256']:
                        raise ValueError('retained case source bytes changed')
                value['case_results'][saved['case_id']]=saved['result']
            outcome=directory/'outcome.json'
            if outcome.exists():
                saved=read_sealed(outcome)
                if saved['product_source_digest']!=digest:raise ValueError('outcome identity mismatch')
                value.update(state=saved['state'],record=saved['record'])
            return value
        except (ValueError,TypeError,KeyError,OSError):
            return {**value,'state':'unknown_or_invalid_evidence','record':None,'case_results':{}}

    def claim(self,digest,identity):
        directory=self.directory(digest)
        try:directory.mkdir()
        except FileExistsError:raise ProductReplayBlocked('product already has a public execution intent')
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        # An empty or partial claim directory after a crash remains blocking.
        sealed_json(directory/'intent.json',{**identity,'schema_version':'openwiki-product-execution-intent/v1',
            'product_source_digest':digest,'state':'unknown_until_outcome'})

    def record_case(self,digest,case_id,result,output: Path):
        if case_id not in ('dev_001','dev_002'):raise ValueError('invalid public case')
        path=self.directory(digest)/('case_'+case_id+'.json')
        if path.exists():
            if read_sealed(path)['result']!=result:raise ValueError('public case result changed after retention')
            return
        references=[]
        for p in sorted(output.rglob('*')):
            if p.is_file() and not p.is_symlink():
                references.append({'relative_path':p.relative_to(output).as_posix(),
                    'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        sealed_json(path,{'schema_version':'openwiki-product-case-retained/v1',
            'product_source_digest':digest,'case_id':case_id,'result':result,
            'evidence_root':str(output),'source_files':references})

    def finish(self,digest,record):
        sealed_json(self.directory(digest)/'outcome.json',{
            'schema_version':'openwiki-product-execution-outcome/v1',
            'product_source_digest':digest,'record':record,
            'state':'completed' if record.get('submission_consumed') is True else 'infrastructure_invalid',
            'automatic_replay_allowed':False})
