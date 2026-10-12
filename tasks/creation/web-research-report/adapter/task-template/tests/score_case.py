#!/usr/bin/env python3
"""Artifact scoreability; independent public bodies are fetched during host staging."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')


def main():
    parser=argparse.ArgumentParser()
    for key in ('cases-root','output-dir','manifest','harness','run-evidence','verifier-dir'):parser.add_argument('--'+key,type=Path,required=True)
    a=parser.parse_args();a.verifier_dir.mkdir(parents=True,exist_ok=True)
    old=[a.verifier_dir/name for name in ('score_contract.json','reward.json','harness_result.json') if (a.verifier_dir/name).exists()]
    if old:
        archive=a.verifier_dir/'previous_attempts'/str(time.time_ns());archive.mkdir(parents=True)
        for path in old:shutil.move(str(path),archive/path.name)
    manifest=json.loads(a.manifest.read_text());case=a.cases_root/manifest['case_id'];fatal=[];quality=[]
    if not a.run_evidence.is_file():write(a.verifier_dir/'infrastructure_error.json',{'errors':['trusted execution evidence missing']});return 2
    ev=json.loads(a.run_evidence.read_text())
    for key,want in (('candidate_exit_code',0),('timed_out',False),('candidate_unchanged',True),('case_unchanged',True),('output_structure_valid',True)):
        if key not in ev:write(a.verifier_dir/'infrastructure_error.json',{'errors':['missing trusted observation '+key]});return 2
        if ev[key]!=want:fatal.append('execution boundary '+key)
    for name in ('report.md','sources.json','evidence_graph.json'):
        try:
            content=(a.output_dir/name).read_text()
            if not content.strip():fatal.append('empty core '+name)
            if name.endswith('.json') and not isinstance(json.loads(content),dict):fatal.append('wrong core type '+name)
        except Exception:fatal.append('missing/unparseable core '+name)
    if not fatal:
        sources=json.loads((a.output_dir/'sources.json').read_text());graph=json.loads((a.output_dir/'evidence_graph.json').read_text())
        if not isinstance(sources.get('sources'),list) or not sources['sources']:fatal.append('no parseable nonempty sources array')
        if any(not isinstance(graph.get(key),list) for key in ('claims','passages','source_relations','calculations')):fatal.append('missing core evidence graph arrays')
        if '#' not in (a.output_dir/'report.md').read_text():fatal.append('not a Markdown research report')
    sys.path.insert(0,str(a.harness.parent));from validate_artifacts import validate
    evaluator_dir=a.harness.parent if (a.harness.parent/'protocol').exists() else a.harness.parent.parent
    try:artifact=validate(case/'input.md',a.output_dir,evaluator_dir)
    except (TypeError,ValueError,AttributeError,KeyError):artifact={'valid':False,'errors':['malformed auxiliary evidence fields']}
    quality.extend(artifact.get('errors',[]));counts=ev.get('provider_counts',{})
    if not isinstance(counts,dict) or any(type(v) is not int or v<0 for v in counts.values()):write(a.verifier_dir/'infrastructure_error.json',{'errors':['malformed trusted provider counts']});return 2
    if counts.get('deepseek',0) or counts.get('gateway',counts.get('gateway_text',0))>300:fatal.append('provider policy/budget violation')
    state='fatal_zero' if fatal else 'scoreable';harness={'case':case.name,'validity_gate':not fatal,'fatal_gate':bool(fatal),'fatal_reasons':fatal,'evaluation_state':state,'quality_errors':quality,'artifact_validation':artifact}
    contract={'schema_version':'1.0','case_id':case.name,'evaluation_mode':manifest['evaluation_mode'],'validity_gate':not fatal,'fatal_gate':bool(fatal),'fatal_reasons':fatal,'evaluation_state':state,'score':0 if fatal else 100,'reward':0.0 if fatal else 1.0,'case_digest':manifest.get('case_digest'),'candidate_digest':manifest.get('candidate_digest'),'output_digest':ev.get('output_digest'),'candidate_exit_code':ev['candidate_exit_code'],'timed_out':ev['timed_out'],'candidate_provider_counts':counts,'trusted_harness_result':harness,'trusted_harness_result_sha256':hashlib.sha256(json.dumps(harness,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'leaderboard_eligible':False}
    write(a.verifier_dir/'harness_result.json',harness);write(a.verifier_dir/'score_contract.json',contract);write(a.verifier_dir/'reward.json',{'score':contract['score'],'reward':contract['reward'],'validity_gate':int(not fatal)});return 0


if __name__=='__main__':raise SystemExit(main())
