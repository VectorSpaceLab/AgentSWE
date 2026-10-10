#!/usr/bin/env python3
"""Single evaluator writer for independently reviewed v2 readiness evidence.

Task runners never invoke this module. It does not run providers or modify
product sources or the configuration registry; admissions reference its current
bytes so other tasks' later admissions cannot invalidate earlier freezes.
"""
from __future__ import annotations
import argparse
import fcntl
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
import audit_readiness as audit
import readiness_admission as admission
from v2_readiness import load_and_validate
from readiness_judge_validation import make_judge_output_validators
from v2_usage_normalizers import make_broker_record_normalizers

ROOT=Path(__file__).resolve().parent

def atomic_bytes(path, payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def encoded(value):
    return (json.dumps(value,indent=2,ensure_ascii=False)+'\n').encode()

def prepare_admission(task, bundle_root, review_path, review_sha256, cfg):
    """Read-only validation; no publication until all external evidence passes."""
    if task not in cfg.TASKS:raise ValueError('unknown task')
    source=Path(cfg.TASKS[task]);contract=source/'meta/0905_case_contract.json'
    smoke_root=cfg.SMOKE_ROOT/task
    bundle_root=Path(bundle_root)
    if bundle_root.is_symlink() or not bundle_root.is_absolute():raise ValueError('invalid bundle root')
    bundle_root.resolve(strict=True).relative_to(smoke_root.resolve(strict=True))
    binding={'task':task,'source_digest':audit.tree_digest(source),
             'contract_digest':admission.sha(contract),
             'registry_digest':admission.registry_digest(ROOT/'configuration_delta_registry.json',task)}
    snapshot=json.loads((ROOT/'post_repair_tree_snapshot.json').read_text())
    if snapshot['tasks'][task]['sibling']['digest']!=binding['source_digest']:
        raise ValueError('current source requires fresh reviewed snapshot')
    manifest=bundle_root/'manifest.json';manifest_sha=admission.sha(manifest)
    review_path=Path(review_path)
    if not review_path.is_absolute() or admission.sha(review_path)!=review_sha256:
        raise ValueError('review report hash mismatch')
    review=json.loads(review_path.read_text())
    if review.get('profile')!=admission.PROFILE or review.get('task')!=task:
        raise ValueError('review profile/task mismatch')
    if review.get('current_binding')!=binding or review.get('manifest_sha256')!=manifest_sha:
        raise ValueError('review source/bundle binding mismatch')
    checks=review.get('review_checks',{})
    if any(checks.get(k) is not True for k in admission.REQUIRED_CHECKS):
        raise ValueError('independent evaluator review incomplete')
    # D54: `notes` carries back the case-deadline tolerance the review exercised, so the
    # published admission states it instead of leaving it only inside the bundle.
    notes={}
    valid,errors=load_and_validate(manifest,bundle_root=bundle_root,
        expected_manifest_sha256=manifest_sha,trusted_current_binding=binding,
        judge_output_validators=make_judge_output_validators(source,
            code_implementation=cfg.AUTHORITATIVE_CREATE_CODE_JUDGE),
        broker_record_normalizers=make_broker_record_normalizers(task),notes=notes)
    if not valid:raise ValueError('readiness evidence rejected: '+'; '.join(errors))
    manifest_bytes=manifest.read_bytes()
    if admission.sha(manifest)!=manifest_sha or audit.tree_digest(source)!=binding['source_digest']:
        raise ValueError('source or evidence changed during verification')
    if admission.sha(contract)!=binding['contract_digest'] or admission.registry_digest(ROOT/'configuration_delta_registry.json',task)!=binding['registry_digest']:
        raise ValueError('contract/registry changed during verification')
    latest=smoke_root/'latest_smoke_manifest.json'
    value={'schema_version':'agentswe-edit-readiness-admission/v2','profile':admission.PROFILE,
           'task':task,'readiness':'READY','sibling_digest':binding['source_digest'],
           'bundle_root':str(bundle_root),'contract':{'path':str(contract),'sha256':binding['contract_digest']},
           'smoke_manifest':{'path':str(latest),'sha256':manifest_sha},
           'review_checks':checks,'review_reports':[{'path':str(review_path),'sha256':review_sha256}],
           'deadline_policy':notes.get('deadline_policy_id'),
           'deadline_rows_accepted':notes.get('deadline_rows_accepted',{})}
    return latest,manifest_bytes,value

def run_audit_locked():
    saved=sys.argv
    try:
        sys.argv=['audit_readiness']
        return audit._main()
    finally:sys.argv=saved

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--task',required=True)
    parser.add_argument('--bundle-root',type=Path,required=True)
    parser.add_argument('--review-report',type=Path,required=True)
    parser.add_argument('--review-sha256',required=True)
    args=parser.parse_args()
    cfg=audit.load_config()
    with (ROOT/'configuration_delta_registry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        latest,payload,value=prepare_admission(args.task,args.bundle_root,args.review_report,args.review_sha256,cfg)
        target=ROOT/'readiness_admissions'/(args.task+'.json')
        previous={p:p.read_bytes() if p.exists() else None for p in (latest,target)}
        try:
            atomic_bytes(latest,payload);atomic_bytes(target,encoded(value))
            run_audit_locked()
            gate=json.loads((ROOT/'formal_readiness_gate.json').read_text())
            row=next(r for r in gate['tasks'] if r['task']==args.task)
            if row['readiness']!='READY' or row['errors']:
                raise ValueError('task admission failed current audit: '+str(row['errors']))
        except BaseException:
            for path,prior in previous.items():
                if prior is None:path.unlink(missing_ok=True)
                else:atomic_bytes(path,prior)
            run_audit_locked()
            raise
        print(json.dumps({'task':args.task,'readiness':'READY','profile':admission.PROFILE,
                          'admission_sha256':admission.sha(target),'ready_count':gate['ready_count']}))
    return 0

if __name__=='__main__':raise SystemExit(main())
