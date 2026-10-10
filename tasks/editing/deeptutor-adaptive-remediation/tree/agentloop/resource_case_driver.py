"""Trusted host driver launched only inside the verified case resource scope."""
import argparse
import json
import shutil
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lower_agent_entry import _run_case, probe_runtime

parser = argparse.ArgumentParser()
parser.add_argument("--repository", type=Path, required=True)
parser.add_argument("--prompt", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--broker-endpoint", required=True)
parser.add_argument("--python", dest="python_executable", required=True)
parser.add_argument('--source-repository',type=Path)
args = parser.parse_args()
source=args.source_repository;del args.source_repository
if source is not None:
    if args.repository.exists():raise RuntimeError('fresh runtime copy destination exists')
    shutil.copytree(source,args.repository,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
    for path in (args.repository,*args.repository.rglob('*')):path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
    (args.output/'source-copy-observation.json').write_text(json.dumps({'source':str(source),'destination':str(args.repository),'cgroup':Path('/proc/self/cgroup').read_text(),'copy_completed_inside_case_scope':True})+'\n')
from import_attribution import suspected, paired_probe, inherited_runner
deadline = float(os.environ['AGENTSWE_CASE_DEADLINE_MONOTONIC'])
probe = probe_runtime(args.python_executable, args.repository, output=args.output / 'runtime_probe.json', _runner=inherited_runner(args.output,deadline))
from protocol import AUTHORITATIVE_SOURCE, AUTHORITATIVE_SOURCE_DIGEST, tree_digest
if suspected(probe):
    if tree_digest(AUTHORITATIVE_SOURCE) != AUTHORITATIVE_SOURCE_DIGEST:
        attribution = {'valid':False,'reason':'authoritative baseline source changed'}
    else:
        attribution = paired_probe(args.python_executable, args.repository, AUTHORITATIVE_SOURCE, args.output/'runtime_probe.json', deadline=deadline)
    (args.output/'import_attribution.json').write_text(json.dumps(attribution)+'\n')
value = _run_case(**vars(args), execute=True)
print(json.dumps(value))
