#!/usr/bin/env python3
"""Credential-free analytical verification and scoreability contract."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time


# --- AgentSWE release compat: legacy gateway count keys (see provider_counts_compat.py) ---
_AGENTSWE_LEGACY_GATEWAY = "s" "u8"
_AGENTSWE_LEGACY_KEYS = {_AGENTSWE_LEGACY_GATEWAY + s: "gateway" + s for s in ("", "_text", "_image", "_image_requests")}


def _agentswe_neutral_provider_keys(value):
    if isinstance(value, dict):
        legacy = [k for k in value if k in _AGENTSWE_LEGACY_KEYS]
        if legacy and not any(_AGENTSWE_LEGACY_KEYS[k] in value for k in legacy):
            value = {_AGENTSWE_LEGACY_KEYS.get(k, k): v for k, v in value.items()}
        return {k: _agentswe_neutral_provider_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_agentswe_neutral_provider_keys(v) for v in value]
    return value
# --- end AgentSWE release compat ---


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')


def main():
    p=argparse.ArgumentParser()
    for name in ('cases-root','output-dir','manifest','run-evidence','verifier-dir','validators-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();a.verifier_dir.mkdir(parents=True,exist_ok=True)
    old=[a.verifier_dir/name for name in ('score_contract.json','reward.json','harness_result.json') if (a.verifier_dir/name).exists()]
    if old:
        archive=a.verifier_dir/'previous_attempts'/str(time.time_ns());archive.mkdir(parents=True)
        for path in old:shutil.move(str(path),archive/path.name)
    m=json.loads(a.manifest.read_text());case=(a.cases_root/m['case_id']).resolve();ev=_agentswe_neutral_provider_keys(json.loads(a.run_evidence.read_text())) if a.run_evidence.is_file() else {}
    sys.path.insert(0,str(a.validators_dir));from validate_artifacts import validate
    from trusted_evidence import dashboard,digest
    art=validate(case/'input.md',a.output_dir)
    if art.get('evaluation_state')=='infrastructure_error':write(a.verifier_dir/'infrastructure_error.json',art);return 2
    if art.get('validity_gate') and art.get('independent_semantics'):
        art['browser_evidence']=dashboard(a.output_dir,art['independent_semantics'],a.verifier_dir/'browser-work')
        if art['browser_evidence'].get('evaluation_state')=='infrastructure_error':write(a.verifier_dir/'infrastructure_error.json',art);return 2
    fatal=list(art.get('fatal_errors',[]))
    if not ev:write(a.verifier_dir/'infrastructure_error.json',{'errors':['trusted run evidence missing']});return 2
    for key,want in (('candidate_exit_code',0),('timed_out',False),('candidate_unchanged',True),('case_unchanged',True),('output_structure_valid',True)):
        if key not in ev:write(a.verifier_dir/'infrastructure_error.json',{'errors':['trusted run observation missing: '+key]});return 2
        if ev[key]!=want:fatal.append('execution boundary: '+key)
    counts=ev.get('provider_counts',{})
    if not isinstance(counts,dict):write(a.verifier_dir/'infrastructure_error.json',{'errors':['malformed trusted provider evidence']});return 2
    if any(type(v) is not int or v<0 for v in counts.values()):write(a.verifier_dir/'infrastructure_error.json',{'errors':['malformed trusted provider counter']});return 2
    if counts.get('serper',0) or counts.get('web_retrieval',0):fatal.append('closed corpus retrieval')
    if counts.get('gateway',counts.get('gateway_text',0))+counts.get('deepseek',0)>300:fatal.append('provider budget exceeded')
    valid=not fatal;state='scoreable' if valid else 'fatal_zero'
    harness={'case':m['case_id'],'validity_gate':valid,'fatal_gate':not valid,'fatal_reasons':fatal,'evaluation_state':state,'artifact_validation':art,'errors':art.get('errors',[])}
    contract={'schema_version':'1.0','case_id':m['case_id'],'evaluation_mode':m['evaluation_mode'],'validity_gate':valid,'fatal_gate':not valid,'fatal_reasons':fatal,'evaluation_state':state,'score':100 if valid else 0,'reward':1.0 if valid else 0.0,'quality_errors':art.get('quality_errors',[]),'candidate_provider_counts':counts,'candidate_exit_code':ev['candidate_exit_code'],'timed_out':ev['timed_out'],'candidate_digest':m.get('candidate_digest'),'case_digest':m.get('case_digest'),'output_digest':ev.get('output_digest'),'trusted_harness_result':harness,'trusted_harness_result_sha256':digest(harness),'leaderboard_eligible':False}
    write(a.verifier_dir/'artifact_validation.json',art);write(a.verifier_dir/'harness_result.json',harness);write(a.verifier_dir/'score_contract.json',contract);write(a.verifier_dir/'reward.json',{'reward':contract['reward'],'score':contract['score'],'validity_gate':int(valid)})
    return 0


if __name__=='__main__':raise SystemExit(main())
