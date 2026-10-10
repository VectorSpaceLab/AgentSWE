#!/usr/bin/env python3
"""Budget freeze of a formal Editing run: freeze the latest accepted submission, run the six held-out cases, finalize.

The Editing protocol ends development when the Builder exits or uses up its submissions or its time budget; the last
accepted submission is then frozen and the six held-out cases run against it. When Harbor cuts the Builder at the 5 h
budget, the native Builder stream has no successful terminal event, and the formal one-stops stop at their lifecycle
gate instead: the run ends without a Result. This tool applies the protocol rule to such a run after the fact,
through the task tree's own code imported as a library (no tree file is edited):

  check     read-only: the trigger, the run's state, the next stage's gates and plan;
  freeze    the tree controller's freeze of the latest accepted submission;
  hidden    the six held-out cases through the tree's hidden runner, with a fresh evaluator-owned lower broker
            started the way the formal path starts it;
  finalize  the tree's evaluator/formal_finalize.py with a fresh Result-judge broker (deepseek-flash / max / 64000,
            as the formal path starts it); summary.json and one_stop_summary.json rewritten the way the formal path
            writes them;
  all       freeze, hidden and finalize in turn.

Without --apply a stage is a dry run: it checks its gates and prints its plan. The trigger (every stage):
  * the formal unit is not running, and no container mounts the run directory;
  * the task tree's digest and control/result_judge.py's sha256 equal those in the run's launch record;
  * the last Builder segment ended as an infrastructure cut with AgentTimeoutError (or at most 600 s before the
    budget deadline) and was not resumed;
  * the stored strict native-Builder proof fails for exactly one reason, the missing successful terminal event;
  * the run's summary status is the tree's lifecycle-gate status;
  * 1 to 5 distinct accepted submissions, the latest one's source digest unchanged since it was accepted;
  * no held-out evidence (until the hidden stage) and no formal_aggregation.json yet.
A run with no accepted submission is refused: it delivers nothing, and its Result is 0 under the protocol.

Each file the tool rewrites is first copied to <name>.pre-manual-freeze; the tool refuses when such a copy exists that
it did not make for this run. The decision and every stage are recorded in <run>/manual_freeze/manual_freeze_record.json
(reason budget_exhausted_freeze_latest_accepted, manual: true, operator, timestamps, per-stage results).
The freeze manifest keeps the freeze reason the tree's own hidden runner and finalizer accept. Aider only: its
finalizer requires builder_session_attestation.same_session; the tool sets it from the tree's verify_native with
allow_interrupted=True (the relaxation the tree grants a max-rounds interrupt), and only when the stored strict proof
fails solely for the missing terminal event; the strict proof stays in native_evidence.

Supported tasks: aider, deeptutor, openwiki. Run it through `agentswe freeze <run_id>`, as the run directory's owner.
It never restarts the Builder, never changes a Candidate byte, never rebinds and never prints a credential.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import contextlib  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pwd  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
TOOL = Path(__file__).resolve()
SUPPORTED = ('aider', 'deeptutor', 'openwiki')
STAGES = ('check', 'freeze', 'hidden', 'finalize', 'all')
REASON = 'budget_exhausted_freeze_latest_accepted'
NO_TERMINAL = 'native Builder has no successful terminal event'
SUFFIX = '.pre-manual-freeze'
DEADLINE_SLACK_SECONDS = 600
MAX_ACCEPTED = 5
MIN_FREE_BYTES = 5 * 2**30
LIVE_UNIT_STATES = ('active', 'activating', 'deactivating', 'reloading')
SYSTEMD_DEFAULT_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
EVIDENCE_DIR = 'manual_freeze'
RECORD_NAME = 'manual_freeze_record.json'
RECORD_SCHEMA = 'agentswe-edit-budget-freeze/v1'
HIDDEN6 = tuple(f'test_{i:03d}' for i in range(1, 7))
DEV2 = ('dev_001', 'dev_002')
PROTOCOL_BASIS = ('Editing protocol: development ends when the Builder exits or uses up its submissions or its time '
                  'budget, and the last accepted submission is frozen and run on the six held-out cases. Harbor cut '
                  'the Builder at the time budget, before the formal path\'s lifecycle gate (which needs a successful '
                  'native terminal event) let the freeze, the held-out cases and the finalizer through.')
NOT_DONE = ['Builder not restarted', 'Candidate bytes not modified', 'task tree and control plane not modified',
            'no rebind']


class Refusal(RuntimeError):
    """A precondition does not hold; nothing (more) was written."""


class NotSupported(Refusal):
    pass


# ------------------------------------------------------------------------------------------------ small helpers --
def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_json_or(path: Path, default=None):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-budget-freeze')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def snapshot(root: Path) -> dict:
    """(size, mtime_ns) of every entry: proves that a read-only stage wrote nothing."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(base, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Refusal(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arg_value(argv: list, flag: str):
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + '='):
            return item.split('=', 1)[1]
    return None


def out(cmd: list) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f'<{type(exc).__name__}>'


def unit_state(unit: str) -> str:
    code, text = out(['systemctl', 'show', '-p', 'ActiveState', '--value', unit])
    return text.strip() if code == 0 and text.strip() else 'unknown'


def unit_recorded_environment(unit: str) -> dict | None:
    """The Environment= systemd still holds for the (ended) unit, or None when the unit is no longer loaded."""
    code, text = out(['systemctl', 'show', '-p', 'Environment', '--value', unit])
    if code != 0 or not text.strip():
        return None
    try:
        items = shlex.split(text.strip())
    except ValueError:
        return None
    return dict(item.split('=', 1) for item in items if '=' in item)


def manager_environment() -> dict:
    """The service manager's environment, which a systemd unit starts from (PATH, LANG...)."""
    code, text = out(['systemctl', 'show-environment'])
    env = {}
    if code == 0:
        for line in text.splitlines():
            if '=' in line and not line.startswith(' '):
                key, value = line.split('=', 1)
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                    env[key] = value
    env.setdefault('PATH', SYSTEMD_DEFAULT_PATH)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP'):  # control_runtime.control_environment
        env.pop(name, None)
    return env


def containers_mounting(run: Path) -> tuple[list | None, str | None]:
    """Containers whose mount source or compose working directory is under the run dir (inspection only)."""
    code, text = out(['docker', 'ps', '-aq', '--no-trunc'])
    if code != 0:
        return None, 'docker ps failed'
    hits = []
    prefix = str(run).rstrip('/') + '/'
    for cid in text.split():
        code, raw = out(['docker', 'inspect', cid])
        if code != 0:
            continue
        try:
            info = json.loads(raw)[0]
        except (ValueError, IndexError):
            continue
        labels = (info.get('Config') or {}).get('Labels') or {}
        paths = [m.get('Source', '') for m in info.get('Mounts') or []]
        paths.append(labels.get('com.docker.compose.project.working_dir', ''))
        if any(p == str(run) or p.startswith(prefix) for p in paths if p):
            hits.append(f"{cid[:12]} {info.get('Name', '')} {(info.get('State') or {}).get('Status', '')}")
    return hits, None


def operator_default() -> str:
    try:
        name = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        name = '?'
    return f'uid {os.geteuid()} ({name})'


# ------------------------------------------------------------------------------------------------- run context --
RUN_NAME = '0905-edit-codex-xhigh-{label}-{task}'


class Run:
    """A formal run directory and everything its own launch and configuration records say about it."""

    def __init__(self, run_dir, control: Path = CONTROL):
        self.control = Path(control)
        if str(self.control) not in sys.path:
            sys.path.append(str(self.control))  # after the tree's own paths (load_one_stop puts those first)
        self.cfg = load_module('budget_freeze_formal_config', self.control / 'formal_config.py')
        self.run = Path(run_dir).resolve()
        task = self.run.parent.name
        if task not in self.cfg.TASKS:
            raise Refusal(f'{self.run} is not a formal Editing run directory (no task {task!r})')
        if task not in SUPPORTED:
            raise NotSupported(f'agentswe freeze does not support the {task} task yet (supported: '
                               f'{", ".join(SUPPORTED)}); its run is left as the formal path ended it')
        self.task = task
        formal = Path(self.cfg.FORMAL_ROOT)
        if self.run.parent != (formal / 'codex_xhigh' / task).resolve():
            raise Refusal(f'{self.run} is not under this install\'s formal run root {formal}/codex_xhigh/{task}')
        match = re.fullmatch(rf'0905-edit-codex-xhigh-(?P<label>.+)-{re.escape(task)}', self.run.name)
        if not match:
            raise Refusal(f'{self.run.name} is not a formal run directory name')
        self.label = match.group('label')
        self.launch_record_path = (formal / 'launch_control' / f'0905-edit-codex-xhigh-{self.label}' / task
                                   / 'launch_record.json')
        if not self.launch_record_path.is_file():
            raise Refusal(f'launch record missing: {self.launch_record_path}')
        self.launch = read_json(self.launch_record_path)
        problems = []
        if self.launch.get('task') != task or self.launch.get('label') != self.label:
            problems.append('task/label')
        if Path(str(self.launch.get('run_dir'))).resolve() != self.run:
            problems.append('run_dir')
        self.unit = str(self.launch.get('unit') or '')
        if not re.fullmatch(rf'agentswe-(oss-)?formal-{re.escape(task)}-{re.escape(self.label)}', self.unit):
            problems.append('unit')
        self.tree = Path(self.cfg.TASKS[task])
        self.command = list(self.launch.get('command') or [])
        one_stop = self.tree / 'harbor' / 'formal_one_stop.py'
        if str(one_stop) not in self.command:
            problems.append('command')
        if problems:
            raise Refusal('the launch record does not describe this run: ' + ', '.join(problems))
        self.one_stop = one_stop
        self.argv = self.command[self.command.index(str(one_stop)) + 1:]
        credential = arg_value(self.argv, '--credential-file')
        if not credential:
            raise Refusal('the launch record command names no --credential-file')
        self.credential = Path(credential)
        self.evidence = self.run / EVIDENCE_DIR
        self.lifecycle = self.run / 'lifecycle'
        self.record_path = self.evidence / RECORD_NAME
        self.applications_log = formal / 'budget_freeze_applications.jsonl'
        self.result_state = None  # what `agentswe result` reports about the cut (--result-state), when given

    @property
    def unit_service(self) -> str:
        return self.unit if self.unit.endswith('.service') else self.unit + '.service'

    def unit_environment(self) -> tuple[dict, str]:
        """The formal unit's --setenv variables: from the launch record, else rebuilt by formal_unit_env.py."""
        recorded = self.launch.get('unit_environment')
        if isinstance(recorded, dict) and recorded:
            return {str(k): str(v) for k, v in recorded.items()}, 'launch_record'
        module = load_module('budget_freeze_formal_unit_env', TOOL.parent / 'formal_unit_env.py')
        gate = self.launch.get('gate') if isinstance(self.launch.get('gate'), dict) else {}
        try:
            env = module.unit_environment(self.task, self.cfg,
                                          integrity_only=gate.get('mode') == module.FORMAL_GATE_RELEASE)
        except FileNotFoundError as exc:
            raise Refusal(f'task environment file missing: {exc}')
        return env, 'rebuilt by formal_unit_env.py (the launch record predates unit_environment)'

    def environment(self) -> tuple[dict, dict]:
        """The process environment of the formal unit: the service manager's plus the unit's variables."""
        unit_env, source = self.unit_environment()
        held = unit_recorded_environment(self.unit_service)
        check = 'unit no longer loaded by systemd'
        if held is not None:
            differs = sorted(k for k in set(unit_env) | set(held) if unit_env.get(k) != held.get(k))
            if differs:
                raise Refusal('the environment differs from the one systemd recorded for the formal unit: '
                              + ', '.join(differs))
            check = 'equal to the Environment systemd recorded for the unit'
        env = {**manager_environment(), **unit_env}
        return env, {'source': source, 'systemd_check': check, 'variables': unit_env}


# ---------------------------------------------------------------------------------------------- tree one-stop --
class _ArgsCaptured(Exception):
    def __init__(self, namespace):
        super().__init__('captured')
        self.namespace = namespace


def load_one_stop(run: Run):
    """The tree's harbor/formal_one_stop.py as a module (definitions only), with the tree first on sys.path."""
    for p in (str(run.tree / 'harbor'), str(run.tree)):
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    return load_module(f'budget_freeze_formal_one_stop_{run.task.replace("-", "_")}', run.one_stop)


def formal_args(fos, argv: list):
    """The argparse namespace the formal unit's one-stop had: its own parser, the launch record's argv, stopped right
    after parsing (the one-stop's main parses first, before any side effect)."""
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        raise _ArgsCaptured(original(self, list(argv), namespace))

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            fos.main(list(argv)) if inspect.signature(fos.main).parameters else fos.main()
        except _ArgsCaptured as captured:
            return captured.namespace
    finally:
        argparse.ArgumentParser.parse_args = original
    raise Refusal('the one-stop main returned without parsing its arguments')


# --------------------------------------------------------------------------------------------- record, backups --
class Ledger:
    """manual_freeze/manual_freeze_record.json: the decision, the backups this freeze made, every stage."""

    def __init__(self, run: Run):
        self.run = run
        self.value = read_json_or(run.record_path) if run.record_path.is_file() else None

    @property
    def exists(self) -> bool:
        return isinstance(self.value, dict)

    def stage(self, name: str) -> dict:
        return ((self.value or {}).get('stages') or {}).get(name) or {}

    def done(self, name: str) -> bool:
        return self.stage(name).get('state') == 'done'

    def owned_backup(self, path: Path) -> bool:
        target = Path(str(path) + SUFFIX)
        for item in (self.value or {}).get('backups') or []:
            if item.get('path') == str(path) and target.is_file() and sha256_file(target) == item.get('sha256'):
                return True
        return False

    def save(self) -> None:
        write_json(self.run.record_path, self.value)

    def begin(self, stage: str) -> None:
        self.value.setdefault('stages', {})[stage] = {'state': 'started', 'started_at': now()}
        self.save()

    def end(self, stage: str, state: str, result: dict) -> None:
        entry = self.value.setdefault('stages', {}).setdefault(stage, {})
        entry.update(state=state, finished_at=now(), result=result)
        self.save()

    def backup(self, path: Path, log: list) -> None:
        """Copy path to path.pre-manual-freeze before its first rewrite; a copy this freeze did not make refuses."""
        path = Path(path)
        target = Path(str(path) + SUFFIX)
        if self.owned_backup(path):
            log.append(f'backup: {path.name}{SUFFIX} already made by this freeze')
            return
        if target.exists() or target.is_symlink():
            raise Refusal(f'backup already exists, refusing to overwrite it: {target}')
        if not path.exists():
            self.value.setdefault('created', []).append(str(path))
            log.append(f'backup: {path.name} does not exist (written new)')
            self.save()
            return
        shutil.copy2(path, target)
        self.value.setdefault('backups', []).append({'path': str(path), 'backup': str(target),
                                                     'sha256': sha256_file(target), 'at': now()})
        self.save()
        log.append(f'backup: {path.name} -> {target.name}')

    def block(self, note: str | None = None) -> dict:
        v = self.value or {}
        block = {'reason': REASON, 'manual': True, 'operator': v.get('operator'), 'frozen_at': v.get('created_at'),
                 'record': str(self.run.record_path), 'tool_sha256': v.get('tool_sha256')}
        if note:
            block['note'] = note
        return block


def preview_backups(paths: list, ledger: Ledger, log: list) -> list:
    """Dry-run view of Ledger.backup: the backups an --apply would make, and refusals it would raise."""
    problems = []
    for path in paths:
        target = Path(str(path) + SUFFIX)
        if ledger.owned_backup(path):
            log.append(f'would keep the backup this freeze made: {target.name}')
        elif target.exists():
            problems.append(f'backup already exists: {target}')
        else:
            log.append(f'would back up {Path(path).name} -> {target.name}' if Path(path).exists()
                       else f'would write {Path(path).name} (new)')
    return problems


# ------------------------------------------------------------------------------------------------- adapters --
class Adapter:
    """Per-tree access to the accepted records, the freeze, the native proof, the held-out runner and finalize."""
    gate_status: tuple = ()
    pre_frozen = False  # the formal path itself froze the latest accepted submission before its gate

    def __init__(self, run: Run, fos, args):
        self.run, self.fos, self.args = run, fos, args

    # --- records and native proof
    def records(self) -> list: raise NotImplementedError
    def frozen_on_disk(self): raise NotImplementedError
    def latest_source(self, record) -> tuple[Path, str | None]: raise NotImplementedError
    def digest(self, path: Path) -> str: raise NotImplementedError
    def strict_native(self) -> dict: raise NotImplementedError
    def freeze_paths(self) -> list: raise NotImplementedError
    def hidden_paths(self) -> list: raise NotImplementedError
    def hidden_attestation(self) -> Path: raise NotImplementedError

    def tree_gates(self, stage: str, facts: dict) -> dict:
        return {}

    def freeze_rewrites(self) -> list:
        return []

    # --- stages
    def do_freeze(self, ledger: Ledger, log: list) -> dict: raise NotImplementedError
    def plan_hidden(self) -> dict: raise NotImplementedError
    def do_hidden(self, ledger: Ledger, log: list) -> dict: raise NotImplementedError
    def finalize_argv(self, endpoint: str) -> list: raise NotImplementedError
    def do_finalize(self, ledger: Ledger, log: list) -> dict: raise NotImplementedError

    # --- shared pieces
    def suffix(self) -> str:
        return hashlib.sha256(str(self.run.run).encode()).hexdigest()[:10]

    def run_finalizer(self, endpoint: str, log: list) -> subprocess.CompletedProcess:
        argv = self.finalize_argv(endpoint)
        log.append('finalizer: ' + ' '.join(argv))
        done = subprocess.run(argv, text=True, capture_output=True, check=False)
        self.run.evidence.mkdir(exist_ok=True)
        (self.run.evidence / 'finalizer.stdout.log').write_text(done.stdout or '')
        (self.run.evidence / 'finalizer.stderr.log').write_text(done.stderr or '')
        return done

    def shared_summary_writer(self):
        """The tree's one_stop_contract.write_summary: what the formal path's atexit hook writes last."""
        import one_stop_contract  # noqa: PLC0415  (the tree's harbor/ is first on sys.path)
        return one_stop_contract.write_summary

    def freeze_summary_note(self, ledger: Ledger, log: list) -> None:
        path = self.run.run / 'summary.json'
        ledger.backup(path, log)
        summary = read_json_or(path, {}) or {}
        summary['manual_freeze'] = ledger.block('frozen under the budget rule; held-out cases and finalize follow')
        write_json(path, summary)


class OpenWiki(Adapter):
    """Controller.freeze(builder_exit_evidence=...) on the retained controller state, the manifest carrying
    freeze_reason budget_exhausted_freeze_latest_accepted and manual_freeze (the openwiki hidden runner and finalizer
    read neither); Controller.hidden(); start_result_judge + formal_finalize.py as run_formal calls them."""
    gate_status = ('builder_lifecycle_incomplete',)
    GUARD = 'openwiki-product-no-replay/v1'

    def _att(self) -> dict:
        return read_json_or(self.run.run / 'builder_session_attestation.json', {}) or {}

    def _state(self) -> dict:
        return read_json_or(self.run.lifecycle / 'controller_state.json', {}) or {}

    def controller(self, endpoint: str = 'http://127.0.0.1:9/unused-after-builder'):
        from agentloop.evaluator.controller import Controller
        c = Controller(self.run.tree / 'input/repository', self.run.tree, self.run.lifecycle, endpoint,
                       builder_session_id=self._att().get('builder_session_id'),
                       hidden_credential_file=self.run.credential.resolve(), hidden_upstream=self.args.upstream,
                       max_dev_rounds=self.args.max_dev_rounds)
        if c.legacy_execution_read_only:
            raise Refusal('the controller loaded the run as legacy read-only')
        return c

    def records(self):
        return self._state().get('records') or []

    def frozen_on_disk(self):
        return self._state().get('frozen') or ((self.run.lifecycle / 'freeze_manifest.json').exists() or None)

    def latest_source(self, record):
        return Path(str(record['candidate_path'])), record.get('candidate_digest')

    def digest(self, path):
        from agentloop.protocol import tree_digest
        return tree_digest(path)

    def strict_native(self):
        return self._att().get('native_evidence') or {}

    def freeze_paths(self):
        lc = self.run.lifecycle
        return [lc / 'freeze_manifest.json', lc / 'freeze_manifest.sha256', lc / 'frozen_candidate']

    def hidden_paths(self):
        lc = self.run.lifecycle
        return [lc / 'hidden-once-gate.json', lc / 'hidden-result.json', lc / 'hidden-after-freeze-attestation.json',
                lc / 'hidden']

    def hidden_attestation(self):
        return self.run.lifecycle / 'hidden-after-freeze-attestation.json'

    def freeze_rewrites(self):
        return [self.run.lifecycle / 'controller_state.json', self.run.run / 'builder_session_attestation.json',
                self.run.run / 'summary.json']

    @staticmethod
    def _accepted_repo(record) -> Path:
        return Path(record['build']['product_entry']).parent.parent

    def tree_gates(self, stage, facts):
        """The accepted history the freeze relies on: the attestation and the controller state agree, and every round's
        submission, build, feedback and Builder binding are intact."""
        from agentloop.protocol import canonical_json, tree_digest
        att, state = self._att(), self._state()
        recs = state.get('records') or []
        session = att.get('builder_session_id')
        g = {
            'attestation.accepted_submission_count == len(controller records)':
                att.get('accepted_submission_count') == len(recs),
            'attestation.candidate_digests_distinct': att.get('candidate_digests_distinct') is True,
            'attestation.accepted_submissions_with_public_dev': att.get('accepted_submissions_with_public_dev') is True,
            'attestation.feedback_chain_consumed': att.get('feedback_chain_consumed') is True,
            'controller_state.builder_session_id == attestation': state.get('builder_session_id') == session,
            f'controller_state.product_execution_guard == {self.GUARD}': state.get('product_execution_guard') == self.GUARD,
            'controller_state readiness_profile/current_binding None':
                state.get('readiness_profile') is None and state.get('current_binding') is None,
            'controller_state.records == attestation.candidate_records': recs == att.get('candidate_records'),
            'rounds 1..N': [r.get('round') for r in recs] == list(range(1, len(recs) + 1)),
        }
        if stage == 'freeze':
            g['attestation.freeze is None (the formal path did not freeze)'] = att.get('freeze') is None
        facts['builder_exit_code'] = att.get('builder_exit_code')
        events = [e for e in att.get('events') or [] if e.get('event') == 'feedback_delivered']
        g['feedback_delivered events carry candidate_number 1..N in order'] = (
            [e.get('candidate_number') for e in events] == list(range(1, len(recs) + 1)))
        for i, r in enumerate(recs, start=1):
            fb = r.get('feedback') or {}
            try:
                payload = json.loads(Path(fb['json']).read_bytes())
                recomputed = hashlib.sha256(canonical_json(
                    {k: v for k, v in payload.items() if k != 'feedback_digest'})).hexdigest()
                fb_ok = recomputed == fb.get('feedback_digest') == payload.get('feedback_digest')
            except Exception:  # noqa: BLE001  (any unreadable feedback fails the gate)
                fb_ok = False
            ev = events[i - 1] if len(events) >= i else {}
            g[f'round {i}: submission tree digest matches'] = (
                Path(str(r.get('candidate_path'))).is_dir() and tree_digest(Path(r['candidate_path'])) == r.get('candidate_digest'))
            g[f'round {i}: build valid, product entry exists'] = (r.get('build') or {}).get('valid') is True and \
                Path(str((r.get('build') or {}).get('product_entry'))).is_file()
            g[f'round {i}: feedback available, not infra-invalid, bytes match digest'] = (
                fb.get('available') is True and fb.get('infrastructure_invalid') is False and fb_ok)
            g[f'round {i}: feedback_delivered event digest matches'] = ev.get('feedback_digest') == fb.get('feedback_digest')
            g[f'round {i}: dev_cases == dev_001,dev_002'] = list(r.get('dev_cases', {})) == list(DEV2)
            g[f'round {i}: same Builder session, builder metadata valid'] = (
                r.get('builder_session_id') == session and (r.get('builder_metadata') or {}).get('valid') is True)
            if i >= 2:
                prev = recs[i - 2]
                rev = r.get('revision') or {}
                g[f'round {i}: bound to the previous feedback'] = (
                    rev.get('parent_candidate_digest') == prev.get('candidate_digest')
                    and rev.get('feedback_digest') == (prev.get('feedback') or {}).get('feedback_digest')
                    and rev.get('feedback_bound_submission') is True and rev.get('same_builder_session') is True)
        if recs and stage == 'freeze':
            latest = recs[-1]
            repo = self._accepted_repo(latest)
            finished = [e for e in att.get('events') or [] if e.get('event') == 'submission_finished'
                        and e.get('candidate_digest') == latest.get('candidate_digest')]
            newest = 0.0
            for base, dirs, files in os.walk(repo):
                for name in dirs + files:
                    with contextlib.suppress(OSError):
                        newest = max(newest, os.lstat(os.path.join(base, name)).st_mtime)
            g['latest accepted repository untouched after its submission_finished event'] = bool(finished) and \
                repo.is_dir() and newest <= datetime.fromisoformat(finished[-1]['at']).timestamp() + 1
            facts['latest_accepted_repository'] = str(repo)
        return g

    def builder_exit_evidence(self) -> dict:
        att = self._att()
        seg = read_json(self.run.run / 'builder_segment_receipt.json')
        last = seg['attempts'][-1]
        exc = last.get('harbor_exception') or {}
        return {'builder_exit_code': att.get('builder_exit_code'), 'native_valid': False,
                'native_errors': self.strict_native().get('errors'),
                'harbor_exception': {k: exc.get(k) for k in ('exception_type', 'exception_message', 'occurred_at')},
                'segment_exit_reason': last.get('exit_reason'),
                'resume_decision': {k: (last.get('decision') or {}).get(k)
                                    for k in ('resume', 'refusals', 'remaining_seconds')},
                'builder_started_at': att.get('started_at'), 'builder_finished_at': att.get('finished_at'),
                'builder_session_id': att.get('builder_session_id')}

    def do_freeze(self, ledger, log):
        from agentloop.protocol import tree_digest
        import agentloop.evaluator.controller as controller_module
        from agentloop.evaluator.hidden_controller import validate_freeze
        att_path = self.run.run / 'builder_session_attestation.json'
        recs = self.records()
        ctrl = self.controller()
        if ctrl.frozen is not None or len(ctrl.records) != len(recs):
            raise Refusal('the retained controller state is not the expected unfrozen history')
        repo_digest = tree_digest(self._accepted_repo(recs[-1]))
        evidence = self.builder_exit_evidence()
        for path in (self.run.lifecycle / 'controller_state.json', att_path):
            ledger.backup(path, log)
        freeze_path = self.run.lifecycle / 'freeze_manifest.json'
        extra = {'freeze_reason': REASON, 'manual_freeze': True,
                 'manual_freeze_operated_at': ledger.value.get('created_at'),
                 'manual_freeze_operator': ledger.value.get('operator'),
                 'manual_freeze_record': str(self.run.record_path)}
        original = controller_module.write_json

        def write_json_with_reason(path, value):
            if Path(path).resolve() == freeze_path.resolve() and isinstance(value, dict):
                value.update(extra)  # the same dict becomes controller.frozen and controller_state.frozen
            return original(path, value)

        controller_module.write_json = write_json_with_reason
        try:
            frozen = ctrl.freeze(builder_exit_evidence=evidence)
        finally:
            controller_module.write_json = original
        validate_freeze(read_json(freeze_path), self.run.lifecycle, freeze_path)
        if self._state().get('frozen') != read_json(freeze_path) or frozen.get('repository_digest') != repo_digest:
            raise Refusal('post-freeze verification failed (state/manifest or repository digest)')
        log.append(f'validate_freeze passed; frozen repository digest {repo_digest[:12]} == latest accepted '
                   f'repository; manifest sha256 {sha256_file(freeze_path)[:12]}')
        att = self._att()
        att['freeze'] = frozen
        att['manual_freeze'] = ledger.block("complete stays false: run_formal's conjunction needs native['valid']; "
                                            'the freeze was made under the budget rule')
        write_json(att_path, att)
        self.freeze_summary_note(ledger, log)
        return {'frozen': frozen, 'freeze_manifest_sha256': sha256_file(freeze_path)}

    def plan_hidden(self):
        return {'runner': 'agentloop.evaluator.controller.Controller.hidden() -> run_hidden_suite(lifecycle/'
                          'freeze_manifest.json, lifecycle/hidden-result.json, <credential>, <tree>, '
                          f'upstream={self.args.upstream})',
                'lower_broker': 'EvaluatorBrokerLifecycle, started inside run_hidden_suite (fresh, evaluator-owned)'}

    def do_hidden(self, ledger, log):
        from agentloop.evaluator.hidden_controller import validate_freeze
        freeze_path = self.run.lifecycle / 'freeze_manifest.json'
        freeze = read_json(freeze_path)
        validate_freeze(freeze, self.run.lifecycle, freeze_path)
        if freeze.get('freeze_reason') != REASON or freeze.get('manual_freeze') is not True:
            raise Refusal('the freeze manifest is not this budget freeze')
        ctrl = self.controller()
        if ctrl.frozen != freeze:
            raise Refusal('controller state frozen != sealed manifest')
        result = ctrl.hidden()
        return {'complete_inventory': result.get('complete_inventory'),
                'formal_result_eligible': result.get('formal_result_eligible'),
                'suite_errors': result.get('suite_errors'),
                'cases': {cid: {k: rec.get(k) for k in ('classification', 'candidate_classification',
                                                       'infrastructure_invalid', 'error')}
                          for cid, rec in (result.get('cases') or {}).items()}}

    def finalize_argv(self, endpoint):
        run = self.run.run
        return [sys.executable, str(self.run.tree / 'evaluator/formal_finalize.py'), '--run-dir', str(run),
                '--output', str(run / 'formal_aggregation.json'), '--credential-file',
                str(self.run.credential.resolve()), '--result-judge-broker-endpoint', endpoint]

    def start_judge(self):
        f = self.fos
        return f.start_result_judge(self.run.evidence, self.run.credential.resolve(), self.args.upstream,
                                    self.args.builder_image)

    def do_finalize(self, ledger, log):
        f, run = self.fos, self.run.run
        if (f.RESULT_JUDGE_MODEL, f.RESULT_JUDGE_EFFORT) != ('deepseek-flash', 'max'):
            raise Refusal('the one-stop Result-judge constants changed')
        for name in ('summary.json', 'one_stop_summary.json'):
            ledger.backup(run / name, log)
        judge, done, cleanup, final_stats = None, None, None, None
        try:
            judge = self.start_judge()
            done = self.run_finalizer(judge.endpoint, log)
            final_stats = f.result_judge_stats(judge.endpoint)
        finally:
            if judge is not None:
                cleanup = judge.close()
        agg = read_json_or(run / 'formal_aggregation.json', {}) or {}
        hidden = read_json_or(self.run.lifecycle / 'hidden-result.json', {}) or {}
        summary = {
            'status': 'completed' if done and done.returncode == 0 else 'formal_finalization_refused',
            'formal_result_claimed': agg.get('formal_result_publishable') is True,
            'code_score_claimed': agg.get('code_score_publishable') is True,
            'builder_session_attestation': self._att(),
            'freeze_manifest': str(self.run.lifecycle / 'freeze_manifest.json'),
            'hidden': hidden, 'formal_aggregation': agg,
            'finalizer_exit_code': done.returncode if done else None,
            'finalizer_stderr_tail': (done.stderr or '')[-1500:] if done else '',
            'result_judge_broker': {'model': f.RESULT_JUDGE_MODEL, 'reasoning_effort': f.RESULT_JUDGE_EFFORT,
                                    'endpoint': judge.endpoint if judge else None,
                                    'stats': str(judge.stats_path) if judge else None,
                                    'lifecycle': str(judge.lifecycle_path) if judge else None},
            'combined_score': None, 'max_dev_rounds': self.args.max_dev_rounds, 'n_concurrent': self.args.n_concurrent,
            'manual_freeze': ledger.block()}
        write_json(run / 'summary.json', summary)
        self.shared_summary_writer()(run, max_dev_rounds=self.args.max_dev_rounds,
                                     n_concurrent=self.args.n_concurrent, mode='formal')
        one = read_json(run / 'one_stop_summary.json')
        one['manual_freeze'] = summary['manual_freeze']
        write_json(run / 'one_stop_summary.json', one)
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg,
                'judge_final_stats': final_stats, 'judge_cleanup': cleanup}


class DeepTutor(Adapter):
    """AcceptedSubmissionController.freeze() on the controller rebuilt from lifecycle/ (its __init__ would rewrite
    builder_session_opened.json); run_hidden with a fresh hidden BrokerProcess; formal_finalize.py with a fresh
    Result-judge BrokerProcess, as main() starts them."""
    gate_status = ('formal_evidence_incomplete',)

    def controller(self):
        import agentloop.two_round_controller as trc
        from agentloop.protocol import DEV_CASES
        opened = read_json(self.run.lifecycle / 'builder_session_opened.json')
        state = read_json(self.run.lifecycle / 'controller_state.json')
        c = trc.AcceptedSubmissionController.__new__(trc.AcceptedSubmissionController)
        c.run_dir = self.run.lifecycle.resolve()
        c.session_id = opened['session_id']
        c.builder_witness = trc._validated_builder_witness(opened['builder_witness'], c.session_id)
        c.builder_witness_digest = trc.object_digest(c.builder_witness)
        if c.builder_witness_digest != opened.get('builder_witness_digest'):
            raise Refusal('the Builder witness digest does not reproduce')
        c.dev_cases = tuple(DEV_CASES)
        c.max_dev_rounds = self.args.max_dev_rounds
        c.pilot_not_formal = False
        c.readiness_profile = None
        c.current_binding = None
        c.records = state.get('records') or []
        c.frozen = state.get('frozen')
        c.feedback_record = state.get('latest_feedback')
        c.infrastructure_attempts = state.get('infrastructure_attempts', 0)
        expected = self._controller_attributes(trc, opened)
        if expected is not None and set(vars(c)) != expected:
            raise Refusal('the rebuilt controller does not carry the attributes the tree controller sets: '
                          + ', '.join(sorted(expected ^ set(vars(c)))))
        if any(r.get('session_id') != c.session_id or r.get('builder_witness_digest') != c.builder_witness_digest
               for r in c.records):
            raise Refusal('the accepted records are not bound to the rebuilt session witness')
        return c

    def _controller_attributes(self, trc, opened):
        """The attribute names the tree's own __init__ sets (built in a scratch directory, never in the run)."""
        try:
            with tempfile.TemporaryDirectory(prefix='budget-freeze-controller-') as scratch:
                probe = trc.AcceptedSubmissionController(Path(scratch), opened['session_id'],
                                                         builder_witness=opened['builder_witness'],
                                                         max_dev_rounds=self.args.max_dev_rounds)
                return set(vars(probe))
        except Exception:  # noqa: BLE001  (the comparison is a fidelity check; the witness gate still applies)
            return None

    def records(self):
        return (read_json_or(self.run.lifecycle / 'controller_state.json', {}) or {}).get('records') or []

    def frozen_on_disk(self):
        state = read_json_or(self.run.lifecycle / 'controller_state.json', {}) or {}
        return state.get('frozen') or ((self.run.lifecycle / 'freeze_manifest.json').exists() or None)

    def latest_source(self, record):
        return Path(str(record['candidate_path'])), record.get('candidate_digest')

    def digest(self, path):
        from agentloop.protocol import tree_digest
        return tree_digest(path)

    def strict_native(self):
        return read_json_or(self.run.run / 'builder_native_attestation.json', {}) or {}

    def tree_gates(self, stage, facts):
        from agentloop.protocol import DEV_CASES
        recs = self.records()
        process = read_json_or(self.run.run / 'builder_process.json', {}) or {}
        facts['builder_exit_code'] = process.get('exit_code')
        facts['lifecycle_ready_for_hidden'] = process.get('lifecycle_ready_for_hidden')
        g = {f'round {r.get("round")}: dev results terminal for {",".join(DEV_CASES)}':
             set(r.get('dev_results', {})) == set(DEV_CASES)
             and all(isinstance(i, dict) and i.get('terminal') is True for i in r.get('dev_results', {}).values())
             for r in recs}
        g['rounds 1..N'] = [r.get('round') for r in recs] == list(range(1, len(recs) + 1))
        return g

    def freeze_paths(self):
        return [self.run.lifecycle / 'frozen_candidate', self.run.lifecycle / 'freeze_manifest.json']

    def hidden_paths(self):
        run = self.run.run
        return [run / 'hidden', run / 'hidden_broker.cid', run / 'hidden_broker_lifecycle.json',
                run / 'hidden_broker_stats.json']

    def hidden_attestation(self):
        return self.run.run / 'hidden' / 'hidden-after-freeze-attestation.json'

    def freeze_rewrites(self):
        return [self.run.lifecycle / 'controller_state.json', self.run.lifecycle / 'builder_session_attestation.json',
                self.run.run / 'summary.json']

    def do_freeze(self, ledger, log):
        ctrl = self.controller()
        if ctrl.frozen is not None:
            raise Refusal('the controller is already frozen')
        for path in (self.run.lifecycle / 'controller_state.json', self.run.lifecycle / 'builder_session_attestation.json'):
            ledger.backup(path, log)
        frozen = ctrl.freeze()  # the formal path calls freeze() without builder_exit_evidence
        manifest = self.run.lifecycle / 'freeze_manifest.json'
        latest = self.records()[-1]
        if frozen.get('candidate_digest') != latest.get('candidate_digest') or \
                self.digest(Path(frozen['candidate_path'])) != latest.get('candidate_digest'):
            raise Refusal('post-freeze verification failed (frozen digest != latest accepted)')
        log.append(f'frozen latest accepted round {latest.get("round")} digest {latest.get("candidate_digest", "")[:12]}')
        self.freeze_summary_note(ledger, log)
        return {'frozen': frozen, 'freeze_manifest_sha256': sha256_file(manifest)}

    def plan_hidden(self):
        return {'lower_broker': "BrokerProcess(run_dir=<run>, role='hidden', max_calls=12, max_tokens=240000)",
                'runtime_python': str(Path(self.args.runtime_python).absolute()),
                'runner': 'agentloop.run_hidden.run(lifecycle/freeze_manifest.json, <run>/hidden, '
                          'case_ids=test_001..test_006)'}

    def start_hidden_broker(self):
        return self.fos.BrokerProcess(run_dir=self.run.run, role='hidden', credential=self.run.credential,
                                      python=self.args.broker_python, max_calls=12, max_tokens=240_000)

    def do_hidden(self, ledger, log):
        f, run = self.fos, self.run.run
        broker, cleanup = None, None
        try:
            broker = self.start_hidden_broker()
            hidden = f.run_hidden(self.run.lifecycle / 'freeze_manifest.json', run / 'hidden',
                                  broker_endpoint=broker.endpoint_local,
                                  python_executable=str(Path(self.args.runtime_python).absolute()),
                                  case_ids=f.HIDDEN_CASES, pilot_not_formal=False)
        finally:
            cleanup = f.stop_brokers((broker,))
        return {'formal_result_eligible': hidden.get('formal_result_eligible'), 'broker_cleanup': cleanup,
                'attestation': str(self.hidden_attestation())}

    def finalize_argv(self, endpoint):
        return [sys.executable, str(self.run.tree / 'evaluator' / 'formal_finalize.py'), '--run-dir', str(self.run.run),
                '--credential-file', str(self.run.credential.resolve()), '--result-judge-broker-endpoint', endpoint]

    def start_judge(self):
        return self.fos.BrokerProcess(run_dir=self.run.evidence, role='result_judge', credential=self.run.credential,
                                      python=self.args.broker_python, max_calls=40, max_tokens=3_000_000,
                                      broker_kind='responses_xhigh')

    def do_finalize(self, ledger, log):
        f, run = self.fos, self.run.run
        hidden = read_json_or(self.hidden_attestation(), {}) or {}
        for name in ('summary.json', 'one_stop_summary.json'):
            ledger.backup(run / name, log)
        judge, done, cleanup = None, None, None
        try:
            self.run.evidence.mkdir(exist_ok=True)
            judge = self.start_judge()
            done = self.run_finalizer(judge.endpoint_local, log)
        finally:
            cleanup = f.stop_brokers((judge,))
        agg = read_json_or(run / 'formal_aggregation.json')
        state = read_json_or(self.run.lifecycle / 'controller_state.json', {}) or {}
        records = state.get('records') or []
        summary = read_json_or(Path(str(run / 'summary.json') + SUFFIX), {}) or {}
        summary.update({
            'status': 'formal_evidence_complete' if hidden.get('formal_result_eligible') else 'formal_evidence_incomplete',
            'candidate_rounds_consumed': len(records),
            'candidate_digests_distinct': len({r.get('candidate_digest') for r in records}) == len(records),
            'freeze_manifest': str(self.run.lifecycle / 'freeze_manifest.json') if state.get('frozen') else None,
            'hidden_attestation': str(self.hidden_attestation()) if self.hidden_attestation().is_file() else None,
            'formal_finalizer_exit': done.returncode if done else None,
            'formal_aggregation': 'formal_aggregation.json' if agg else None,
            'formal_result_claimed': bool(agg and agg.get('formal_result_publishable')),
            'code_score_claimed': bool(agg and agg.get('code_score_publishable')),
            'result_axis': agg.get('result_axis') if agg else 'N/A', 'code_axis': agg.get('code_axis') if agg else 'N/A',
            'combined_score': None, 'max_dev_rounds': self.args.max_dev_rounds, 'n_concurrent': self.args.n_concurrent,
            'manual_freeze': ledger.block()})
        write_json(run / 'summary.json', summary)
        write_json(run / 'one_stop_summary.json', summary)
        self.shared_summary_writer()(run, max_dev_rounds=self.args.max_dev_rounds,
                                     n_concurrent=self.args.n_concurrent, mode='formal')
        one = read_json(run / 'one_stop_summary.json')
        one['manual_freeze'] = summary['manual_freeze']
        write_json(run / 'one_stop_summary.json', one)
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg, 'judge_cleanup': cleanup}


class Aider(Adapter):
    """The formal path froze the latest accepted submission itself (freeze_latest('builder_exit'), unconditional):
    the freeze stage verifies that freeze and sets same_session from the tree's verify_native(allow_interrupted=True).
    Held-out cases: Controller.run_hidden() with a fresh lower broker; finalize: formal_finalize.py with a fresh
    Result-judge broker, as _run_formal starts them."""
    gate_status = ('builder_integration_incomplete',)
    pre_frozen = True

    def _att(self) -> dict:
        return read_json_or(self.run.run / 'builder_session_attestation.json', {}) or {}

    def _lifecycle(self) -> dict:
        return read_json_or(self.run.lifecycle / 'dev_lifecycle.json', {}) or {}

    def controller(self, endpoint=None):
        from harbor.agentloop_controller import Controller
        c = Controller(self.run.lifecycle, self.run.tree / 'input/repository', endpoint, True,
                       dependency_overlay=None, image=self.args.lower_image, public_cases=DEV2, hidden_cases=HIDDEN6,
                       pilot_not_formal=False, max_dev_rounds=self.args.max_dev_rounds)
        # _run_formal sets these after constructing the controller (the product context binds the constructor's)
        c.dependency_overlay = Path(self.args.dependency_overlay).resolve() if self.args.dependency_overlay else None
        return c

    def records(self):
        return self._lifecycle().get('records') or []

    def frozen_on_disk(self):
        return self._lifecycle().get('frozen')

    def latest_source(self, record):
        materialized = Path(record.get('materialized_path')
                            or self.run.lifecycle / f"materialized_{int(record['submission_number']):03d}")
        frozen = self.frozen_on_disk() or {}
        return materialized, frozen.get('candidate_materialized_digest')

    def digest(self, path):
        from harbor.agentloop_controller import digest
        return digest(path)

    def strict_native(self):
        return self._att().get('native_evidence') or {}

    def native_proof(self, allow_interrupted: bool) -> dict:
        """BuilderLifecycle.native_attestation (the tree's own method) on the session rebuilt from the run's files:
        the accepted records, the feedback_delivered events and the native thread observations."""
        holder = types.SimpleNamespace(run_dir=self.run.run, events=self._att().get('events') or [],
                                       native_observations=read_json_or(
                                           self.run.run / 'builder_native_observations.json', []) or [],
                                       controller=types.SimpleNamespace(records=self.records()))
        return self.fos.BuilderLifecycle.native_attestation(holder, allow_interrupted=allow_interrupted)

    def tree_gates(self, stage, facts):
        recs = self.records()
        frozen = self.frozen_on_disk() or {}
        digests = [r.get('candidate_digest') for r in recs]
        att = self._att()
        facts['builder_exit_code'] = att.get('builder_exit_code')
        g = {'formal-path freeze present (unconditional builder_exit freeze)': bool(frozen),
             'freeze bound to the latest accepted submission and the whole accepted ledger': bool(frozen) and bool(recs)
             and frozen.get('candidate_delivery_digest') == recs[-1].get('candidate_digest')
             and frozen.get('accepted_candidate_digests') == digests
             and frozen.get('accepted_submission_count') == len(recs),
             "freeze reason is the tree's builder_exit": frozen.get('freeze_reason') == 'builder_exit',
             'frozen_candidate digest == freeze manifest': bool(frozen) and (self.run.lifecycle / 'frozen_candidate').is_dir()
             and self.digest(self.run.lifecycle / 'frozen_candidate') == frozen.get('candidate_digest'),
             'public dev complete for every accepted round': bool(recs) and all(
                 self.fos.public_round_complete(r) for r in recs)}
        if stage == 'freeze':
            g['attestation.same_session is not yet set'] = att.get('same_session') is False
            try:
                strict = self.native_proof(False)
                relaxed = self.native_proof(True)
            except Exception as exc:  # noqa: BLE001
                facts['native_replay'] = f'{type(exc).__name__}: {exc}'
                strict = relaxed = {'valid': False, 'errors': [f'replay failed: {type(exc).__name__}']}
            facts['native_replay'] = {'strict': {'valid': strict.get('valid'), 'errors': strict.get('errors')},
                                      'allow_interrupted': {'valid': relaxed.get('valid'), 'errors': relaxed.get('errors')}}
            g['strict native proof replayed from the run files reproduces the stored errors'] = (
                strict.get('errors') == self.strict_native().get('errors'))
            g['verify_native(allow_interrupted=True) valid (feedback chain, product bound)'] = (
                relaxed.get('valid') is True and not relaxed.get('errors'))
            self._relaxed = relaxed
        return g

    def freeze_paths(self):
        return []  # already frozen by the formal path

    def hidden_paths(self):
        run = self.run.run
        return [run / 'brokers/hidden.cid', run / 'hidden_broker_initial.json',
                self.run.lifecycle / 'hidden-after-freeze-attestation.json', self.run.lifecycle / 'evaluations/hidden']

    def hidden_attestation(self):
        return self.run.lifecycle / 'hidden-after-freeze-attestation.json'

    def freeze_rewrites(self):
        return [self.run.run / 'builder_session_attestation.json', self.run.run / 'summary.json']

    def do_freeze(self, ledger, log):
        relaxed = getattr(self, '_relaxed', None) or self.native_proof(True)
        if not (relaxed.get('valid') is True and not relaxed.get('errors')):
            raise Refusal('verify_native(allow_interrupted=True) did not pass: ' + json.dumps(relaxed.get('errors'))[:400])
        frozen = self.frozen_on_disk()
        log.append("the formal path had already frozen the latest accepted submission (freeze_latest('builder_exit'));"
                   ' verified, not re-frozen')
        path = self.run.run / 'builder_session_attestation.json'
        ledger.backup(path, log)
        att = self._att()
        att['same_session'] = True
        att['native_evidence_budget_interrupted'] = relaxed
        att['manual_freeze'] = ledger.block('same_session set from verify_native(allow_interrupted=True), the '
                                            'relaxation the formal path grants a max_dev_rounds interrupt; the strict '
                                            'proof, failing only on the missing terminal event, stays in native_evidence')
        write_json(path, att)
        self.freeze_summary_note(ledger, log)
        return {'frozen': frozen, 'freeze_manifest_sha256': sha256_file(self.run.lifecycle / 'freeze_manifest.json'),
                'same_session_from': 'verify_native(allow_interrupted=True)'}

    def plan_hidden(self):
        return {'lower_broker': {'name': 'aider-formal-hidden-' + self.suffix(),
                                 'script': str(self.run.tree / 'evaluator/broker/lower_responses_broker.py'),
                                 'effort': self.fos.LOWER_EFFORT, 'cidfile': str(self.run.run / 'brokers/hidden.cid')},
                'lower_image': self.args.lower_image,
                'dependency_overlay': str(Path(self.args.dependency_overlay).resolve()) if self.args.dependency_overlay else None,
                'runner': 'harbor.agentloop_controller.Controller.run_hidden() (test_001..test_006)'}

    def start_hidden_broker(self):
        f = self.fos
        plan = self.plan_hidden()['lower_broker']
        port = f.port()
        f.start_broker(name=plan['name'], script=Path(plan['script']), credential=self.run.credential.resolve(),
                       value_port=port, effort=f.LOWER_EFFORT, cidfile=Path(plan['cidfile']))
        return plan['name'], Path(plan['cidfile']), f'http://127.0.0.1:{port}/v1/responses'

    def do_hidden(self, ledger, log):
        f, run = self.fos, self.run.run
        cleanup, name, cid = None, 'aider-formal-hidden-' + self.suffix(), run / 'brokers/hidden.cid'
        try:
            name, cid, endpoint = self.start_hidden_broker()
            initial = f.stats(endpoint)
            if int((initial.get('runtime') or {}).get('calls', 0)) != 0:
                raise Refusal('the formal hidden broker must start at zero calls')
            write_json(run / 'hidden_broker_initial.json', {'started_after_freeze': True, 'independent_from_public': True,
                                                            'stats': initial, 'manual_freeze': True})
            ctrl = self.controller(endpoint)
            if not ctrl.frozen:
                raise Refusal('the controller state is not frozen')
            results = ctrl.run_hidden()
        finally:
            cleanup = f.cleanup_owned_containers({'hidden': name}, {'hidden': True}, {'hidden': cid})
        att = read_json_or(self.hidden_attestation(), {}) or {}
        return {'cases': [{k: r.get(k) for k in ('case_id', 'classification', 'exit_code')} for r in results],
                'attestation_complete': att.get('complete'), 'broker_cleanup': cleanup}

    def finalize_argv(self, endpoint):
        return ['python3', str(self.run.tree / 'evaluator/formal_finalize.py'), '--run-dir', str(self.run.run),
                '--credential-file', str(self.run.credential.resolve()), '--result-judge-broker-endpoint', endpoint]

    def start_judge(self):
        f = self.fos
        name = 'aider-formal-result-judge-' + self.suffix() + '-mf'
        cid = self.run.evidence / 'result_judge.cid'
        port = f.port()
        f.start_broker(name=name, script=f.JUDGE_BROKER_SCRIPT, credential=self.run.credential.resolve(),
                       value_port=port, effort=f.BUILDER_EFFORT, cidfile=cid)
        return name, cid, f'http://127.0.0.1:{port}/v1/responses'

    def do_finalize(self, ledger, log):
        f, run = self.fos, self.run.run
        for name_ in ('summary.json', 'one_stop_summary.json'):
            ledger.backup(run / name_, log)
        name, cid = 'aider-formal-result-judge-' + self.suffix() + '-mf', self.run.evidence / 'result_judge.cid'
        done, cleanup = None, None
        try:
            self.run.evidence.mkdir(exist_ok=True)
            name, cid, endpoint = self.start_judge()
            done = self.run_finalizer(endpoint, log)
            try:
                f.save_role_stats(self.run.evidence, 'result_judge', endpoint, 'direct')
            except Exception as exc:  # noqa: BLE001  (stats are diagnostic; the finalizer output is authoritative)
                log.append(f'judge stats not saved: {type(exc).__name__}')
        finally:
            cleanup = f.cleanup_owned_containers({'result_judge': name}, {'result_judge': True}, {'result_judge': cid})
        out_path = run / 'formal_aggregation.json'
        agg = read_json(out_path) if out_path.is_file() else {
            'formal_result_publishable': False, 'result_axis': 'N/A', 'code_axis': 'N/A',
            'reasons': [f'formal finalizer failed with exit {done.returncode if done else None}']}
        ctrl = self.controller()
        hidden_att = read_json_or(self.hidden_attestation(), {}) or {}
        att = self._att()
        block = ledger.block()
        write_json(run / 'summary.json', {
            'status': 'formal_result_ready' if agg.get('formal_result_publishable') else 'lifecycle_complete_result_not_publishable',
            'formal_result_claimed': agg.get('formal_result_publishable') is True,
            'code_score_claimed': agg.get('code_score_publishable') is True,
            'builder_session_id': att.get('builder_session_id'), 'records': ctrl.records, 'freeze': ctrl.frozen,
            'hidden': hidden_att.get('results'), 'formal_aggregation': agg,
            'finalizer_exit_code': done.returncode if done else None, 'manual_freeze': block})
        write_json(run / 'one_stop_summary.json', {
            'status': 'completed' if agg.get('formal_result_publishable') else 'formal_result_not_publishable',
            'dev_lifecycle': ctrl.records, 'freeze': ctrl.frozen,
            'hidden': {'inventory': list(HIDDEN6), 'summary': hidden_att.get('results')},
            'result_judge_contracts': agg.get('result_judge_contracts'), 'code_contract': agg.get('code_contract'),
            'combined_score': None, 'cleanup_attestation': str(run / 'builder_container_cleanup.json'),
            'manual_freeze': block})
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg, 'judge_cleanup': cleanup}


ADAPTERS = {'aider': Aider, 'deeptutor': DeepTutor, 'openwiki': OpenWiki}


# ---------------------------------------------------------------------------------------------------- gates --
def trigger_gates(run: Run, ad: Adapter, stage: str, ledger: Ledger, apply: bool = False) -> tuple[dict, dict]:
    """Every precondition of `stage` (freeze, hidden or finalize), as name -> bool, plus facts for the record."""
    g, facts = {}, {}
    rd = run.run
    owner = rd.stat().st_uid
    facts['run_owner_uid'], facts['euid'] = owner, os.geteuid()
    g['run directory owned by the invoking user'] = owner == os.geteuid()
    state = unit_state(run.unit_service)
    facts['unit'], facts['unit_state'] = run.unit_service, state
    g['formal unit not running'] = state not in LIVE_UNIT_STATES
    audit = load_module('budget_freeze_audit_readiness', run.control / 'audit_readiness.py')
    tree_digest_now = audit.tree_digest(run.tree)
    facts['tree_digest'] = tree_digest_now
    g['task tree digest == launch record sibling_digest'] = tree_digest_now == run.launch.get('sibling_digest')
    g['control/result_judge.py sha256 == launch record'] = (
        sha256_file(run.control / 'result_judge.py') == run.launch.get('result_judge_sha256'))
    seg = read_json_or(rd / 'builder_segment_receipt.json', {}) or {}
    attempts = [a for a in seg.get('attempts') or [] if isinstance(a, dict)]
    promoted = [a for a in attempts if a.get('promoted')]  # the segments the session kept (builder_segments.py)
    last = promoted[-1] if promoted else (attempts[-1] if attempts else {})
    exc = last.get('harbor_exception') or {}
    deadline, ended = seg.get('builder_deadline_epoch'), last.get('ended_at_epoch')
    remaining = (deadline - ended) if isinstance(deadline, (int, float)) and isinstance(ended, (int, float)) else None
    facts['segment'] = {'count': len(attempts), 'exit_reason': last.get('exit_reason'),
                        'exit_code': last.get('exit_code'), 'exception': exc.get('exception_type'),
                        'seconds_before_deadline_at_end': remaining,
                        'resume': (last.get('decision') or {}).get('resume')}
    g['Builder cut at the budget (last segment infrastructure_cut; AgentTimeoutError or <= 600 s before the deadline; '
      'not resumed)'] = bool(
        last.get('exit_reason') == 'infrastructure_cut'
        and (exc.get('exception_type') == 'AgentTimeoutError'
             or (remaining is not None and remaining <= DEADLINE_SLACK_SECONDS))
        and (last.get('decision') or {}).get('resume') is False)
    strict = ad.strict_native()
    facts['strict_native_errors'] = strict.get('errors')
    g['stored strict native proof fails only on the missing terminal event'] = (
        strict.get('valid') is False and strict.get('errors') == [NO_TERMINAL])
    summary = read_json_or(rd / 'summary.json', {}) or {}
    facts['summary_status'] = summary.get('status')
    g[f'summary status is the lifecycle-gate status ({", ".join(ad.gate_status)})'] = summary.get('status') in ad.gate_status
    recs = ad.records()
    facts['accepted_submissions'] = len(recs)
    g['1..5 accepted submissions'] = 1 <= len(recs) <= MAX_ACCEPTED
    digests = [r.get('candidate_digest') for r in recs]
    g['accepted submission digests distinct'] = len(digests) == len(set(digests))
    if recs:
        source, expected = ad.latest_source(recs[-1])
        facts['latest_accepted'] = {'round': recs[-1].get('round', recs[-1].get('submission_number')),
                                    'candidate_digest': recs[-1].get('candidate_digest'), 'source': str(source)}
        got = ad.digest(source) if source.is_dir() else None
        facts['latest_accepted']['source_digest'] = got
        g['latest accepted source present and its digest unchanged since acceptance'] = (
            got is not None and expected is not None and got == expected)
    g.update(ad.tree_gates(stage, facts))
    frozen = ad.frozen_on_disk()
    facts['frozen_on_disk'] = bool(frozen)
    if stage == 'freeze':
        reported = run.result_state
        if reported is None:
            facts['agentswe_result'] = 'not compared (the tool was run without --result-state)'
        else:
            g['agentswe result reports this run as cut by the Builder budget'] = reported.get('detected') is True
            if reported.get('detected') is True:
                g['accepted submissions agree with agentswe result'] = (
                    reported.get('accepted_submissions') == len(set(digests)))
                latest = (reported.get('latest_accepted') or {}).get('digest')
                if latest and recs:
                    g['latest accepted submission agrees with agentswe result'] = latest == recs[-1].get('candidate_digest')
            facts['agentswe_result'] = reported
        g['no budget freeze record yet'] = not ledger.exists
        g[f'no {EVIDENCE_DIR}/ directory from an earlier tool'] = not run.evidence.exists()
        if not ad.pre_frozen:
            g['not yet frozen'] = not frozen and not any(p.exists() for p in ad.freeze_paths())
    else:
        g['budget freeze record present, freeze stage done'] = ledger.exists and ledger.done('freeze') and \
            (ledger.value or {}).get('reason') == REASON and (ledger.value or {}).get('schema_version') == RECORD_SCHEMA
        g['frozen'] = bool(frozen)
    if stage in ('freeze', 'hidden'):
        present = [str(p) for p in ad.hidden_paths() if p.exists()]
        facts['hidden_evidence_present'] = present
        g['no held-out evidence yet'] = not present
    if stage == 'finalize':
        g['hidden stage done, held-out attestation present'] = ledger.done('hidden') and ad.hidden_attestation().is_file()
    g['no formal_aggregation.json yet'] = not (rd / 'formal_aggregation.json').exists()
    hits, error = containers_mounting(rd)
    facts['containers_mounting_run'] = hits if hits is not None else error
    g['no container mounts this run'] = hits == []
    if stage in ('freeze', 'hidden'):
        g['>= 5 GiB free on the run filesystem'] = shutil.disk_usage(rd).free >= MIN_FREE_BYTES
    if stage in ('hidden', 'finalize'):
        if apply:
            g['credential file present (written by agentswe freeze)'] = run.credential.is_file()
        else:
            facts['credential'] = 'written by agentswe freeze for an --apply of this stage'
    return g, facts


def next_stage(ad: Adapter, ledger: Ledger, run: Run) -> str | None:
    if not ledger.done('freeze'):
        return 'freeze'
    if not ledger.done('hidden'):
        return 'hidden'
    if not ledger.done('finalize') and not (run.run / 'formal_aggregation.json').exists():
        return 'finalize'
    return None


def zero_accepted(ad: Adapter) -> bool:
    return len(ad.records()) == 0


ZERO_MESSAGE = ('no accepted submission: there is nothing to freeze. Under the Editing protocol a Builder whose '
                'submissions were never accepted delivers nothing, and the run\'s Result is 0.')


# --------------------------------------------------------------------------------------------------- stages --
def print_gates(g: dict, log: list, indent: str = '  ') -> list:
    failed = []
    for name, ok in g.items():
        log.append(f'{indent}{"ok  " if ok else "FAIL"} {name}')
        if not ok:
            failed.append(name)
    return failed


def stage_check(run: Run, ad: Adapter, ledger: Ledger, env_info: dict, log: list) -> int:
    before = snapshot(run.run)
    log.append(f'task {run.task}  label {run.label}  unit {run.unit_service}')
    log.append(f'environment: {env_info["source"]}; {env_info["systemd_check"]}')
    if zero_accepted(ad):
        log.append('REFUSED: ' + ZERO_MESSAGE)
        return 1
    stage = next_stage(ad, ledger, run)
    code = 0
    if stage is None:
        log.append('the budget freeze is complete: freeze, hidden and finalize are recorded in ' + str(run.record_path))
    else:
        g, facts = trigger_gates(run, ad, stage, ledger)
        log.append(f'next stage: {stage}')
        failed = print_gates(g, log)
        log.append('facts: ' + json.dumps(facts, ensure_ascii=False, default=str)[:3000])
        if stage == 'freeze':
            problems = preview_backups(ad.freeze_rewrites(), ledger, log)
            failed += problems
            latest = facts.get('latest_accepted') or {}
            log.append(f'would freeze: latest accepted submission {latest.get("round")} '
                       f'(delivery digest {latest.get("candidate_digest")})' if not ad.pre_frozen else
                       'would verify the formal-path freeze of the latest accepted submission and set same_session')
        log.append('hidden plan: ' + json.dumps(ad.plan_hidden(), default=str))
        log.append('finalize argv: ' + ' '.join(ad.finalize_argv('<fresh Result-judge broker endpoint>')))
        code = 1 if failed else 0
        log.append('the next stage may run' if not failed else 'REFUSED: ' + '; '.join(failed))
    after = snapshot(run.run)
    changed = sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})
    log.append(f'run directory untouched by this check: {not changed}' + (f'; CHANGED: {changed[:5]}' if changed else ''))
    return 3 if changed else code


def stage_run(run: Run, ad: Adapter, ledger: Ledger, stage: str, apply: bool, operator: str, env_info: dict,
              log: list) -> int:
    if zero_accepted(ad):
        raise Refusal(ZERO_MESSAGE)
    g, facts = trigger_gates(run, ad, stage, ledger, apply)
    log.append(f'-- stage {stage}: gates')
    failed = print_gates(g, log)
    if stage == 'freeze':
        failed += preview_backups(ad.freeze_rewrites(), ledger, log if not apply else [])
    if failed:
        raise Refusal('preconditions failed: ' + '; '.join(failed))
    if not apply:
        if stage == 'freeze':
            log.append('would ' + ('verify the formal-path freeze and set same_session' if ad.pre_frozen else
                                   'freeze the latest accepted submission through the tree controller')
                       + ', and record the decision in ' + str(run.record_path))
        elif stage == 'hidden':
            log.append('would start a fresh evaluator-owned lower broker and run: ' + json.dumps(ad.plan_hidden(), default=str))
        else:
            log.append('would start a fresh Result-judge broker (deepseek-flash / max / 64000) and run: '
                       + ' '.join(ad.finalize_argv('<endpoint>')))
        log.append('DRY RUN: nothing written or started')
        return 0
    run.evidence.mkdir(exist_ok=True)
    if stage == 'freeze':
        ledger.value = {'schema_version': RECORD_SCHEMA, 'task': run.task, 'run_dir': str(run.run),
                        'run_id': run.run.name, 'reason': REASON, 'manual': True, 'operator': operator,
                        'operator_uid': os.geteuid(), 'created_at': now(), 'tool': str(TOOL),
                        'tool_sha256': sha256_file(TOOL), 'protocol_basis': PROTOCOL_BASIS, 'not_done': NOT_DONE,
                        'launch_record': {'path': str(run.launch_record_path),
                                          'sha256': sha256_file(run.launch_record_path)},
                        'unit': run.unit_service, 'environment': env_info, 'trigger': {'gates': g, 'facts': facts},
                        'backups': [], 'stages': {}}
        ledger.save()
    ledger.begin(stage)
    try:
        result = {'freeze': ad.do_freeze, 'hidden': ad.do_hidden, 'finalize': ad.do_finalize}[stage](ledger, log)
    except BaseException as exc:
        ledger.end(stage, 'failed', {'error': f'{type(exc).__name__}: {exc}'[:2000]})
        raise
    ok = True
    if stage == 'hidden':
        ok = bool(ad.hidden_attestation().is_file())
        write_json(run.evidence / 'hidden_stage_record.json', {'at': now(), **result})
    if stage == 'finalize':
        ok = result.get('finalizer_exit') == 0
        write_json(run.evidence / 'finalize_stage_record.json', {'at': now(), **result})
        agg = result.get('aggregation') or {}
        log.append(json.dumps({'finalizer_exit': result.get('finalizer_exit'),
                               'formal_result_publishable': agg.get('formal_result_publishable'),
                               'result_axis': agg.get('result_axis'), 'reasons': agg.get('reasons')},
                              ensure_ascii=False, default=str)[:2000])
    ledger.end(stage, 'done', {k: v for k, v in result.items() if k in (
        'freeze_manifest_sha256', 'same_session_from', 'formal_result_eligible', 'complete_inventory', 'suite_errors',
        'attestation_complete', 'finalizer_exit')} | ({'frozen_candidate_digest': (result.get('frozen') or {}).get(
            'candidate_digest')} if stage == 'freeze' else {}) | ({'result_axis': (result.get('aggregation') or {}).get(
                'result_axis')} if stage == 'finalize' else {}))
    if stage == 'freeze':
        frozen = result.get('frozen') or {}
        log.append(f'FROZEN: candidate_digest {frozen.get("candidate_digest")} source_submission '
                   f'{frozen.get("source_submission")}')
    return 0 if ok else 2


def execute(run: Run, stage: str, apply: bool, operator: str, env_info: dict, log: list,
            adapter_factory=None) -> int:
    """Load the tree's one-stop, rebuild the formal unit's arguments, and run one stage (or all)."""
    fos = load_one_stop(run)
    args = formal_args(fos, run.argv)
    ad = (adapter_factory or ADAPTERS[run.task])(run, fos, args)
    ledger = Ledger(run)
    if ledger.exists and (ledger.value.get('reason') != REASON or ledger.value.get('schema_version') != RECORD_SCHEMA):
        raise Refusal(f'{run.record_path} exists and is not a record of this budget freeze')
    if stage == 'check':
        return stage_check(run, ad, ledger, env_info, log)
    stages = [stage] if stage != 'all' else ['freeze', 'hidden', 'finalize']
    code = 0
    for name in stages:
        if stage == 'all' and ledger.done(name):
            log.append(f'-- stage {name}: already done')
            continue
        code = stage_run(run, ad, ledger, name, apply, operator, env_info, log)
        if code != 0 or not apply:
            if not apply and stage == 'all' and name == 'freeze' and code == 0:
                log.append('(hidden and finalize are checked when they are next; a dry run of `all` stops here)')
            break
    return code


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run-dir', required=True, type=Path)
    p.add_argument('--stage', choices=STAGES, default='check')
    p.add_argument('--apply', action='store_true', help='execute the stage (default: a dry run)')
    p.add_argument('--operator', default='', help='who decided the freeze (default: the invoking user)')
    p.add_argument('--result-state', default=None,
                   help="what `agentswe result` reports about the run's budget cut, as JSON (agentswe freeze passes "
                        'it); the freeze stage requires that report to agree with its own trigger')
    p.add_argument('--clean-environment', action='store_true', help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    operator = a.operator.strip() or operator_default()
    try:
        run = Run(a.run_dir)
        env, env_info = run.environment()
        if a.result_state is not None:
            try:
                run.result_state = json.loads(a.result_state)
            except ValueError:
                raise Refusal('--result-state is not JSON')
            if not isinstance(run.result_state, dict):
                raise Refusal('--result-state is not a JSON object')
    except Refusal as exc:
        print(f'REFUSED: {exc}', flush=True)
        return 1
    if not a.clean_environment:
        # Run as the formal unit ran: its interpreter flags, its environment, its working directory.
        python = str(run.cfg.CONTROL_PYTHON)
        os.chdir(run.tree)
        os.execve(python, [python, '-E', '-s', '-B', str(TOOL), *(argv if argv is not None else sys.argv[1:]),
                           '--clean-environment'], env)
    os.chdir(run.tree)
    lock = Path(tempfile.gettempdir()) / f'agentswe-budget-freeze-{hashlib.sha256(str(run.run).encode()).hexdigest()[:16]}.lock'
    with open(lock, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('REFUSED: another budget freeze of this run is in progress', flush=True)
            return 1
        log: list = []
        print(f'== budget freeze  stage={a.stage}  mode={"APPLY" if a.apply and a.stage != "check" else "READ-ONLY"}'
              f'  {now()}\n   run={run.run}', flush=True)
        try:
            code = execute(run, a.stage, a.apply and a.stage != 'check', operator, env_info, log)
        except Refusal as exc:
            for line in log:
                print('   ' + line)
            print(f'REFUSED: {exc}', flush=True)
            return 1
        for line in log:
            print('   ' + line)
        if a.apply and a.stage != 'check':
            entry = {'at': now(), 'task': run.task, 'run_dir': str(run.run), 'stage': a.stage, 'exit': code,
                     'operator': operator, 'tool_sha256': sha256_file(TOOL)}
            with contextlib.suppress(OSError):
                run.evidence.mkdir(exist_ok=True)
                with (run.evidence / 'tool_invocations.log').open('a') as fh:
                    fh.write(json.dumps({**entry, 'log': log}, ensure_ascii=False) + '\n')
            with contextlib.suppress(OSError):
                with run.applications_log.open('a') as fh:
                    fh.write(json.dumps(entry) + '\n')
        print(f'== exit {code}', flush=True)
        return code


if __name__ == '__main__':
    raise SystemExit(main())
