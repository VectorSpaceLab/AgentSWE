#!/usr/bin/env python3
"""Target host: regenerate 87-shared-window/freeze_baseline.json for the current trees.
usage: freeze_baseline_target.py <label> "<note>"  -- backs up the previous file as .pre-<label>"""
import hashlib, json, os, socket, subprocess, sys, shutil
from pathlib import Path
H = Path('@@AGENTSWE_EDITING_CONTROL@@'); sys.path.insert(0, str(H))
import formal_config as cfg
from audit_readiness import tree_digest
from readiness_admission import registry_digest
from readiness_binding import verify_binding
label, note = sys.argv[1], sys.argv[2]
FB = Path('@@AGENTSWE_LEGACY_DATA@@/0921-fixes/87-shared-window/freeze_baseline.json')
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
registry = H / 'configuration_delta_registry.json'; snapshot = H / 'post_repair_tree_snapshot.json'
out = {'schema_version': 'agentswe-edit-0921b-freeze-baseline/v1', 'label': label,
       'generated_at': subprocess.run(['date','-u','+%Y-%m-%dT%H:%M:%SZ'],capture_output=True,text=True).stdout.strip(),
       'host': socket.gethostname(), 'note': note, 'tasks': {}, 'shared_files': {}, 'registry': {}, 'gate': None}
for task, src in sorted(cfg.TASKS.items()):
    src = Path(src)
    binding = {'task': task, 'source_digest': tree_digest(src),
               'contract_digest': sha(src / 'meta/0905_case_contract.json'),
               'registry_digest': registry_digest(registry, task)}
    verify_binding(src, binding, control_root=H)
    out['tasks'][task] = {'sibling': str(src), 'tree_digest': binding['source_digest'],
                          'contract_digest': binding['contract_digest'],
                          'registry_digest': binding['registry_digest'], 'verify_binding': 'passed'}
reg = json.loads(registry.read_bytes())
pinned = sorted({ref['path'] for row in reg['tasks'].values() for ref in row.get('effective_source_files', [])
                 if ref['path'].startswith(str(H) + '/')})
for path in pinned: out['shared_files'][os.path.basename(path)] = sha(path)
for extra in ('execution_scoring.py','formal_config.py','readiness_binding.py','readiness_admission.py','audit_readiness.py'):
    out['shared_files'].setdefault(extra, sha(H / extra))
out['registry'] = {'configuration_delta_registry.json': sha(registry), 'post_repair_tree_snapshot.json': sha(snapshot),
                   'pinned_control_plane_files': len(pinned), 'rows': sorted(reg['tasks'])}
if FB.is_file(): shutil.copy2(FB, FB.with_name(FB.name + '.pre-' + label))
FB.write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')
print('written', FB, 'tasks', len(out['tasks']), 'shared', len(out['shared_files']), 'host', out['host'])
