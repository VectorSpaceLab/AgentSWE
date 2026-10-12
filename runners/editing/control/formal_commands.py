"""Shared reviewed command construction for validator and actual launcher."""
from pathlib import Path
import hashlib

from control_runtime import control_command

HARBOR = Path('@@AGENTSWE_HARBOR_BIN@@')
ISSUED_ROOT = Path('@@AGENTSWE_EDITING_RUNS@@/formal/evaluator-issued')
DEEPCODE_PYTHON = Path('@@AGENTSWE_ENVS@@/deepcode-runtime-venv/bin/python')


def hidden_bundle_path(task: str, run_dir: Path) -> Path:
    return ISSUED_ROOT / task / run_dir.name


def build_formal_command(task: str, sibling: Path, run_dir: Path,
                         credential: Path, python: Path) -> list[str]:
    command = control_command(python, sibling / 'harbor/formal_one_stop.py')
    command.extend(['--benchmark', str(sibling)] if task == 'codex' else ['--run-formal'])
    command.extend(['--run-dir', str(run_dir), '--credential-file', str(credential),
                    '--max-dev-rounds', '5', '--n-concurrent', '1',
                    '--harbor', str(HARBOR), '--builder-timeout',
                    '18000'])  # 5 h Builder cap; ai-scientist task timeout floor lowered to match
    if task in {'claude', 'dyad'}:
        command.extend(['--hidden-cases-dir', str(hidden_bundle_path(task, run_dir))])
    if task == 'deepcode':
        command.extend(['--python', str(DEEPCODE_PYTHON)])
    if task == 'openclaw':
        command.append('--run-hidden')
    return command


def branch_input_evidence(task: str, sibling: Path, run_dir: Path) -> dict:
    """Check pre-issued inputs before starting any paid branch.

    Each task's one-stop independently validates its scenario/authority schema.
    This launcher-level check records actual files and refuses missing inputs.
    It never creates worlds or copies historical results.
    """
    paths = [HARBOR]
    if task == 'deepcode':
        paths.append(DEEPCODE_PYTHON)
    if task in {'claude', 'dyad'}:
        bundle = hidden_bundle_path(task, run_dir)
        if not bundle.is_absolute() or bundle.resolve().is_relative_to(sibling.resolve()):
            raise ValueError('hidden input bundle must be evaluator-owned outside the task')
        paths.extend(bundle / f'test_{index:03d}.json' for index in range(1, 7))
        if task == 'claude':
            paths.extend(bundle / f'test_{index:03d}.oracle.json' for index in range(1, 7))
    evidence=[]
    for path in paths:
        if not path.is_file():raise ValueError('missing formal prerequisite: '+str(path))
        evidence.append({'path':str(path),'resolved_path':str(path.resolve()),
                         'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'task':task,'files':evidence,'provider_calls':0}
