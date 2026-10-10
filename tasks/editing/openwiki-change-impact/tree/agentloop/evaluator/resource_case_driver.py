"""Trusted controller of one resource-bound product and native observation."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.lower_agent_launcher import _run_case,runtime_preflight
from agentloop.evaluator.semantic_oracle import observe
from agentloop.protocol import write_json

parser = argparse.ArgumentParser()
parser.add_argument('--repository', type=Path, required=True)
parser.add_argument('--request', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--endpoint', required=True)
parser.add_argument('--timeout', type=float, required=True)
parser.add_argument('--deadline-monotonic', type=float, default=None)
parser.add_argument('--working-directory', type=Path)
parser.add_argument('--fixture-case-id')
parser.add_argument('--fixture-cases-root',type=Path)
parser.add_argument('--fixture-output',type=Path)
args = parser.parse_args()
if args.fixture_case_id:
    from agentloop.evaluator.fixture_service import materialize_runtime_case
    fixture=materialize_runtime_case(args.fixture_case_id,args.fixture_cases_root,args.fixture_output)
    args.working_directory=fixture['repository']
    write_json(args.output/'fixture-materialization.json',{'runtime':{k:str(v) if isinstance(v,Path) else v for k,v in fixture.items()},
        'cgroup':Path('/proc/self/cgroup').read_text(),'inside_case_scope':True,'fixture_is_not_solution':True})
runtime_preflight(args.repository,args.output)
value = _run_case(args.repository, args.request, args.output, args.endpoint, args.timeout, args.working_directory,
    deadline_monotonic=args.deadline_monotonic)
case_id = args.request.parent.name if args.request.name in {'input.md', 'request.json'} else args.request.stem.split('.', 1)[0]
if case_id in ('dev_001', 'dev_002', *(f'test_{i:03d}' for i in range(1, 7))) and (args.output / 'workspace').is_dir():
    source = args.working_directory or args.repository
    native = observe(case_id, ROOT / ('dev_cases' if case_id.startswith('dev_') else 'test_cases'), source, args.output / 'workspace')
    path = args.output / 'native-semantic-comparison.json'
    write_json(path, native)
    value.update(native_semantic_comparison_path=str(path),
        native_semantic_comparison_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        semantic_observer_in_case_budget=True)
if (args.output/'fixture-materialization.json').is_file():value['runtime_fixture']=json.loads((args.output/'fixture-materialization.json').read_text())['runtime']
write_json(args.output / 'launcher_result.json', value)
print(json.dumps(value))
