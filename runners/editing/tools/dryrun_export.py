"""Run a task's bundle exporter against a finished run, purely to surface errors.

Nothing is admitted and no gate is written: this only calls export() so every
remaining defect shows up in one pass instead of costing one run each.
"""
import argparse, importlib.util, json, shutil, sys, traceback, uuid
from pathlib import Path

CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
sys.path.insert(0, str(CONTROL))

ap = argparse.ArgumentParser()
ap.add_argument('--task', required=True)
ap.add_argument('--run-dir', required=True)
args = ap.parse_args()

import formal_config as cfg
from v2_usage_normalizers import make_broker_record_normalizers

source = Path(cfg.TASKS[args.task])
run = Path(args.run_dir).resolve()
binding = json.loads((run / 'readiness_current_binding.json').read_bytes())

cleanup = next((p for p in (run / 'coordinator_cleanup').glob('cleanup.json')), None)
if cleanup is None:
    print('no coordinator cleanup receipt; run finalize once first'); raise SystemExit(2)

spec = importlib.util.spec_from_file_location('task_readiness_bundle',
                                              source / 'evaluator/readiness_bundle.py')
sys.path.insert(0, str(source))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

dest = run / ('dryrun_bundle_' + uuid.uuid4().hex)
try:
    result = module.export(run, dest, cleanup_receipt=cleanup, trusted_binding=binding,
                           broker_normalizers=make_broker_record_normalizers(args.task))
except Exception as exc:
    print('EXPORT FAILED: %s: %s' % (type(exc).__name__, exc))
    tb = traceback.format_exc().splitlines()
    for line in tb[-6:]:
        print('   ', line)
    shutil.rmtree(dest, ignore_errors=True)
    raise SystemExit(1)

print('export OK ->', result['bundle_root'])
from v2_readiness import load_and_validate
from readiness_judge_validation import make_judge_output_validators
valid, errors = load_and_validate(
    Path(result['bundle_root']) / 'manifest.json', bundle_root=Path(result['bundle_root']),
    expected_manifest_sha256=result['manifest_sha256'], trusted_current_binding=binding,
    judge_output_validators=make_judge_output_validators(
        source, code_implementation=cfg.AUTHORITATIVE_CREATE_CODE_JUDGE),
    broker_record_normalizers=make_broker_record_normalizers(args.task))
print('validation:', valid)
for e in errors:
    print('   ', e)
shutil.rmtree(dest, ignore_errors=True)
