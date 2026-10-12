#!/bin/bash
# usage: wait_admit_only.sh <task> <readiness-tag>   -- waits for the readiness unit, admits, never launches
cd @@AGENTSWE_EDITING_TOOLS@@
t=$1; tag=$2; u=agentswe-edit-$t-$tag; d=@@AGENTSWE_EDITING_RUNS@@/smoke/$t/$tag
echo "== waiting for $u $(date +%T)"
# wait up to 90 min for the unit to appear (launchers are slow under load); an exit receipt means it already ran
for i in $(seq 1 180); do systemctl is-active -q $u && break; [ -f $d/unit_exit_receipt.json ] && break; sleep 30; done
while systemctl is-active -q $u; do sleep 120; done
echo "== $u ended $(date +%T): $(systemctl is-active $u)"
ok=$(python3 -c "
import json,os
d='$d'
for n in ('summary.json','one_stop_summary.json','readiness_summary.json'):
    p=os.path.join(d,n)
    if os.path.exists(p):
        x=json.load(open(p)); st=x.get('status'); err=x.get('errors')
        if st and str(st) in ('completed', 'pilot_pipeline_complete', 'readiness_evidence_complete', 'pilot_complete', 'pilot_pipeline_complete', 'lifecycle_complete'): print('yes'); break
        if n=='readiness_summary.json' and not err: print('yes'); break
else: print('no')
" 2>/dev/null); echo "ok: $ok"
if [ "$ok" = yes ]; then
  python3 admit.py --task $t --run-dir $d --unit $u --binding-file @@AGENTSWE_LEGACY_DATA@@/0915-edit/$t-$tag/readiness_current_binding.json --apply 2>&1 | grep -vE '^\s*$' | tail -6 | cut -c1-200
  python3 -c "import json;g=json.load(open('@@AGENTSWE_EDITING_CONTROL@@/formal_readiness_gate.json'));print('gate:', [x['readiness'] for x in g['tasks'] if x['task']=='$t'][0], g['ready_count'])"
else
  python3 -c "import json;x=json.load(open('$d/summary.json'));print({k:x.get(k) for k in ('status','error_type','error_detail')})" 2>/dev/null
fi
echo "== done $(date +%T)"
