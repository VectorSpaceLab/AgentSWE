#!/usr/bin/python3 -B
"""Lite Editing: budget exhausted -> freeze the latest accepted Candidate -> 6 hidden -> finalize.

Protocol (the paper's Editing freeze rule): when the Builder exits OR its budget is exhausted,
the latest accepted Candidate is frozen and the six hidden cases run against it.  Harbor cuts the
Builder at the 18000 s budget (infrastructure_cut / AgentTimeoutError); each tree's formal path then
refuses to go on because its gate needs a successful native terminal event:
  aider         freezes anyway (freeze_latest('builder_exit') is unconditional) but stops at
                builder_integration_incomplete: same_session = native['valid'] is false.
  ai-scientist  freeze_after_builder_exit() only freezes on exit 0 -> builder_integration_incomplete.
  deeptutor     freezes only when native['valid'] -> formal_evidence_incomplete, hidden not started.
This tool does, after the fact, what the formal path would have done had that gate let it through,
through each tree's OWN code (controller freeze, broker starters, hidden runner, formal_finalize.py),
modelled on @@AGENTSWE_EDITING_TOOLS@@/manual_freeze_budget_exhausted.py (openwiki).

It never restarts the Builder, never edits a Candidate byte, never writes under @@AGENTSWE_LEGACY_HOME@@ (tree or
control plane), never rebinds and never prints a credential.  Every pre-existing file it rewrites is
first copied to <name>.pre-manual-freeze (refusing if that backup exists).  The manual decision is
recorded as reason 'budget_exhausted_freeze_latest_accepted', manual: true, in
<run>/manual_freeze/manual_freeze_record.json, in builder_session_attestation.json ('manual_freeze')
and in summary.json.  The freeze manifest keeps the tree's own freeze_reason ('builder_exit'): the
trees' hidden runners and finalizers accept only {'builder_exit', 'max_dev_rounds'}.

aider only: its finalizer requires builder_session_attestation.same_session == True.  The tool sets it
from the tree's verify_native(..., allow_interrupted=True) -- the relaxation the tree already grants a
max_dev_rounds interrupt -- and only when the strict proof failed for exactly one reason (no
successful terminal event).  The strict proof stays in native_evidence; both are recorded.

usage (as root, /usr/bin/python3 -B):
  lite_manual_freeze.py --task T --run-dir R --stage check                      # read-only, any run
  lite_manual_freeze.py --task T --run-dir R --stage freeze   --dry-run | --apply --operator "..."
  lite_manual_freeze.py --task T --run-dir R --stage hidden   --dry-run | --apply
  lite_manual_freeze.py --task T --run-dir R --stage finalize --dry-run | --apply
--apply is accepted only for a formal Lite cell (…/codex_xhigh/<task>/0905-edit-codex-xhigh-lite-v1-e-<task>-…).
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
BENCH = Path('@@AGENTSWE_EDITING_SOURCES@@')
FORMAL_ROOT = Path('@@AGENTSWE_EDITING_RUNS@@/formal/codex_xhigh')
CELLS = Path('@@AGENTSWE_LEGACY_DATA@@/lite-v1/editing/cells')
APPLY_LOG = Path('@@AGENTSWE_LEGACY_DATA@@/lite-v1/editing/manual_freeze_applications.jsonl')
CREDENTIAL = Path('@@AGENTSWE_CREDENTIAL_FILE@@')  # path only; never read here
TREES = {
    'aider': BENCH / '12-edit-aider-worktree-transaction-agentloop-v1',
    'ai-scientist': BENCH / '16-edit-ai-scientist-reproducibility-gate-v5-agentloop-v1',
    'deeptutor': BENCH / '18-edit-deeptutor-adaptive-remediation-v5-agentloop-v1',
}
REASON = 'budget_exhausted_freeze_latest_accepted'
SUFFIX = '.pre-manual-freeze'
NO_TERMINAL = 'native Builder has no successful terminal event'
HIDDEN6 = tuple(f'test_{i:03d}' for i in range(1, 7))
DEV2 = ('dev_001', 'dev_002')
MAX_DEV_ROUNDS = 5
READINESS_PROFILE = 'single-dev-two-round-hidden-smoke-v1'
TOOL = Path(__file__).resolve()


class Refusal(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-manual-freeze')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def snapshot(root: Path) -> dict:
    """(size, mtime_ns) of every entry: proves a read-only stage wrote nothing."""
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


# --------------------------------------------------------------------------- run context --
class Ctx:
    def __init__(self, task: str, run: Path):
        self.task, self.run, self.tree = task, run.resolve(), TREES[task]
        m = re.fullmatch(rf'{re.escape(str(FORMAL_ROOT))}/{re.escape(task)}/0905-edit-codex-xhigh-'
                         rf'(lite-v1-e-{re.escape(task)}-.+)-{re.escape(task)}', str(self.run))
        self.kind = 'formal' if m else ('readiness' if (self.run / 'readiness_current_binding.json').is_file() else 'other')
        self.label = m.group(1) if m else None
        self.unit = (f'agentswe-formal-{task}-{self.label}' if m
                     else f'agentswe-edit-{task}-{self.run.name}')
        self.launch_record = (Path('@@AGENTSWE_EDITING_RUNS@@/formal/launch_control')
                              / f'0905-edit-codex-xhigh-{self.label}' / task / 'launch_record.json') if m else None
        self.unit_env_file = CELLS / f'{self.label}.unit_env.json' if m else None
        self.binding = read_json(self.run / 'readiness_current_binding.json') if self.kind == 'readiness' else None
        self.evidence = self.run / 'manual_freeze'
        self.lifecycle = self.run / 'lifecycle'

    @property
    def formal(self) -> bool:
        return self.kind == 'formal'


def load_unit_env(ctx: Ctx, log: list) -> None:
    """Run with exactly the formal unit's environment (captured at launch into cells/<label>.unit_env.json)."""
    if not ctx.formal:
        return
    if not ctx.unit_env_file.is_file():
        raise Refusal(f'unit environment record missing: {ctx.unit_env_file}')
    env = read_json(ctx.unit_env_file)['environment']
    for key, value in env.items():
        if key in os.environ and os.environ[key] != value:
            raise Refusal(f'environment differs from the formal unit: {key}')
        os.environ[key] = value
    log.append(f'environment = formal unit environment ({len(env)} keys, incl. '
               f'AGENTSWE_EDIT_EARLY_STOP_RESAMPLE={env.get("AGENTSWE_EDIT_EARLY_STOP_RESAMPLE")})')


def load_fos(ctx: Ctx):
    """Import the tree's harbor/formal_one_stop.py as a module (definitions only; no side effects)."""
    for p in (str(ctx.tree / 'harbor'), str(ctx.tree)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location('lite_mf_formal_one_stop', ctx.tree / 'harbor/formal_one_stop.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------- adapters --
class Adapter:
    """Per-tree access to accepted records, freeze, native evidence, hidden and finalize."""
    summary_incomplete: set = set()

    def __init__(self, ctx: Ctx, fos):
        self.ctx, self.fos = ctx, fos

    # records / freeze on disk
    def records(self) -> list: raise NotImplementedError
    def frozen_on_disk(self): raise NotImplementedError
    def latest_source(self, record) -> tuple[Path, str]: raise NotImplementedError
    def digest(self, path: Path) -> str: raise NotImplementedError
    def strict_native(self) -> dict: raise NotImplementedError
    def controller(self, endpoint: str = 'http://127.0.0.1:9/unused-after-builder'): raise NotImplementedError
    def freeze_paths(self) -> list: raise NotImplementedError
    def do_freeze(self, log: list) -> dict: raise NotImplementedError
    def hidden_paths(self) -> list: raise NotImplementedError
    def plan_hidden(self) -> dict: raise NotImplementedError
    def do_hidden(self, log: list) -> dict: raise NotImplementedError
    def finalize_plan(self, endpoint: str) -> list: raise NotImplementedError
    def do_finalize(self, log: list) -> dict: raise NotImplementedError


class AiScientist(Adapter):
    summary_incomplete = {'builder_integration_incomplete'}

    def _kwargs(self, judge_endpoint=None):
        f = self.fos
        if self.ctx.kind == 'readiness':
            return dict(image=f.LOWER_IMAGE, public_case_ids=('dev_001',), hidden_case_ids=('test_001',),
                        run_kind='pilot', max_dev_rounds=2, n_concurrent=1, result_judge_endpoint=None,
                        readiness_profile=READINESS_PROFILE, current_binding=self.ctx.binding)
        return dict(image=f.LOWER_IMAGE, public_case_ids=DEV2, hidden_case_ids=HIDDEN6, run_kind='formal',
                    max_dev_rounds=MAX_DEV_ROUNDS, n_concurrent=1, result_judge_endpoint=judge_endpoint)

    def controller(self, endpoint='http://127.0.0.1:9/unused-after-builder', judge_endpoint=None):
        return self.fos.Controller(self.fos.ROOT, self.ctx.lifecycle, endpoint, False, **self._kwargs(judge_endpoint))

    def records(self):
        return read_json(self.ctx.lifecycle / 'dev_lifecycle.json').get('records') or []

    def frozen_on_disk(self):
        return read_json(self.ctx.lifecycle / 'dev_lifecycle.json').get('frozen')

    def latest_source(self, record):
        paths = record.get('attempt_paths')
        repo = (Path(paths['repository']) if isinstance(paths, dict)
                else self.ctx.lifecycle / 'build' / f"candidate_{int(record['submission_number']):03d}" / 'repository')
        return repo, record.get('build', {}).get('candidate_repo_digest')

    def digest(self, path):
        from agentloop.protocol import tree_digest
        return tree_digest(path)

    def strict_native(self):
        return (read_json(self.ctx.run / 'builder_session_attestation.json').get('native_evidence') or {})

    def freeze_paths(self):
        return [self.ctx.lifecycle / 'frozen_candidate', self.ctx.lifecycle / 'freeze_manifest.json']

    def do_freeze(self, log):
        run = self.ctx.run
        for path in (self.ctx.lifecycle / 'dev_lifecycle.json', run / 'builder_session_attestation.json'):
            backup(path, True, log)
        ctrl = self.controller()
        if ctrl.frozen is not None:
            raise Refusal('controller already frozen')
        frozen = ctrl.freeze_latest('builder_exit')
        att = read_json(run / 'builder_session_attestation.json')
        att['freeze'] = frozen
        att['freeze_reason'] = frozen.get('freeze_reason')
        rc = frozen.get('revision_contract') or {}
        att['revision_contract'] = {
            'requires_feedback_bound_later_distinct_candidate': len(ctrl.records) > 1,
            'later_distinct_candidate_present': bool(rc.get('later_distinct_candidate_present')),
            'feedback_consumed': bool(rc.get('feedback_consumed')),
            'empty_feedback_attestation': False}
        att['manual_freeze'] = manual_block(self.ctx, 'complete stays false: it is run_formal\'s own conjunction and '
                                            'native[\'valid\'] is false; the freeze was made manually under the '
                                            'budget-exhaustion rule')
        write_json(run / 'builder_session_attestation.json', att)
        return frozen

    def hidden_paths(self):
        run = self.ctx.run
        return [run / 'brokers/hidden_lower.cid', run / 'brokers/hidden_lower-lower-transport',
                run / 'hidden_broker_before.json', self.ctx.evidence / 'public_judge.cid',
                self.ctx.lifecycle / 'hidden-after-freeze-attestation.json', self.ctx.lifecycle / 'hidden']

    def _suffix(self):
        return hashlib.sha256(str(self.ctx.run).encode()).hexdigest()[:10]

    def plan_hidden(self):
        f = self.fos
        return {'broker': f'ai-scientist-formal-hidden-lower-{self._suffix()}', 'script': str(f.LOWER_BROKER),
                'effort': f.LOWER_EFFORT, 'image': f.BUILDER_IMAGE, 'cidfile': str(self.ctx.run / 'brokers/hidden_lower.cid'),
                'judge_for_build_failure_scoring': f'ai-scientist-formal-dev-judge-{self._suffix()}-mf '
                                                   '(the formal path keeps its public judge alive through hidden)',
                'runner': 'agentloop.two_round_controller.Controller.run_hidden(test_001..test_006)'}

    def do_hidden(self, log):
        f, run = self.fos, self.ctx.run
        plan = self.plan_hidden()
        cid, jcid = Path(plan['cidfile']), self.ctx.evidence / 'public_judge.cid'
        port, jport = free_port(), free_port()
        endpoint = f'http://127.0.0.1:{port}/v1/responses'
        jendpoint = f'http://127.0.0.1:{jport}/v1/responses'
        receipt = jreceipt = None
        try:
            f.start_broker(f'ai-scientist-formal-dev-judge-{self._suffix()}-mf', f.JUDGE_BROKER_SCRIPT, CREDENTIAL,
                           jport, f.BUILDER_EFFORT, f.BUILDER_IMAGE, jcid)
            f.start_broker(plan['broker'], f.LOWER_BROKER, CREDENTIAL, port, f.LOWER_EFFORT, f.BUILDER_IMAGE, cid)
            before = f.broker_stats(endpoint)
            if f.broker_call_count(before) != 0 or f.broker_failure_count(before) != 0:
                raise Refusal('fresh hidden lower broker did not start at calls=0/failures=0')
            write_json(run / 'hidden_broker_before.json', before)
            backup(run / 'protocol_lock.json', True, log)
            protocol = read_json(run / 'protocol_lock.json')
            protocol['lower_agent'].update(hidden_broker_endpoint=endpoint, hidden_broker_initial_calls=0,
                                           hidden_broker_initial_failures=0, controller_endpoint_switched_after_freeze=True,
                                           manual_freeze=True)
            write_json(run / 'protocol_lock.json', protocol)
            ctrl = self.controller(endpoint, jendpoint)
            if ctrl.frozen is None:
                raise Refusal('controller state is not frozen')
            hidden = ctrl.run_hidden(HIDDEN6)
        finally:
            receipt = f.remove_and_verify_container('hidden_lower', cid, attempted=cid.exists())
            jreceipt = f.remove_and_verify_container('public_judge', jcid, attempted=jcid.exists())
        return {'hidden': hidden, 'broker_cleanup': [receipt, jreceipt],
                'attestation': read_json(self.ctx.lifecycle / 'hidden-after-freeze-attestation.json')}

    def finalize_plan(self, endpoint):
        f = self.fos
        return [sys.executable, str(f.ROOT / 'evaluator' / 'formal_finalize.py'), '--run-dir', str(self.ctx.run),
                '--credential-file', str(CREDENTIAL), '--result-broker-endpoint', endpoint,
                '--result-judge', str(f.SHARED_RESULT_JUDGE), '--code-judge', str(f.CREATE_CODE_JUDGE)]

    def do_finalize(self, log):
        f, run = self.fos, self.ctx.run
        hidden_att = read_json(self.ctx.lifecycle / 'hidden-after-freeze-attestation.json')
        if not f.hidden_evidence_ready_for_finalizer(hidden_att):
            raise Refusal('hidden evidence is not ready for the finalizer (tree gate)')
        cid = run / 'brokers/judge.cid'
        name = f'ai-scientist-formal-result-judge-{self._suffix()}'
        if cid.exists():
            cid, name = self.ctx.evidence / 'judge.cid', name + '-mf'
        port = free_port()
        endpoint = f'http://127.0.0.1:{port}/v1/responses'
        done, receipt, stats = None, None, None
        try:
            f.start_broker(name, f.JUDGE_BROKER_SCRIPT, CREDENTIAL, port, f.BUILDER_EFFORT, f.BUILDER_IMAGE, cid)
            done = subprocess.run(self.finalize_plan(endpoint), text=True, capture_output=True, check=False)
            stats = f.broker_stats(endpoint)
        finally:
            receipt = f.remove_and_verify_container('judge', cid, attempted=cid.exists())
        write_log(self.ctx, done)
        agg = read_json(run / 'formal_aggregation.json') if (run / 'formal_aggregation.json').is_file() else None
        backup(run / 'judge_broker_stats.json', True, log)
        write_json(run / 'judge_broker_stats.json', stats or {})
        complete = hidden_att.get('formal_complete') is True
        for name_ in ('summary.json', 'one_stop_summary.json'):
            backup(run / name_, True, log)
        summary = read_json(run / 'summary.json') if (run / 'summary.json').is_file() else {}
        summary.update({
            'schema_version': 'agentswe-ai-scientist-formal-summary-v1', 'evidence_kind': 'formal',
            'status': 'lifecycle_complete' if complete else 'lifecycle_evidence_invalid_or_candidate_failed',
            'formal_result_claimed': bool(agg and agg.get('formal_result_publishable')),
            'code_score_claimed': bool(agg and agg.get('code_score_publishable')),
            'formal_finalizer_invoked': True, 'formal_finalizer_exit': done.returncode if done else None,
            'builder_session_attestation': 'builder_session_attestation.json',
            'freeze_manifest': 'lifecycle/freeze_manifest.json',
            'hidden_attestation': 'lifecycle/hidden-after-freeze-attestation.json',
            'formal_aggregation': 'formal_aggregation.json' if agg else None,
            'result_axis': agg.get('result_axis') if agg else 'N/A', 'code_axis': agg.get('code_axis') if agg else 'N/A',
            'manual_freeze': manual_block(self.ctx)})
        write_json(run / 'summary.json', summary)
        freeze = read_json(self.ctx.lifecycle / 'freeze_manifest.json')
        write_json(run / 'one_stop_summary.json', {
            'schema_version': 'agentswe-edit-one-stop-summary-v1', 'status': summary['status'],
            'dev_lifecycle': 'lifecycle/dev_lifecycle.json',
            'freeze': {'path': 'lifecycle/freeze_manifest.json', 'digest': freeze.get('candidate_materialized_digest'),
                       'reason': freeze.get('freeze_reason')},
            'hidden_inventory': list(HIDDEN6), 'hidden_summary': summary.get('hidden', 'N/A'),
            'result_judge_contracts': (agg or {}).get('result_judge_contracts', {}),
            'code_contract': (agg or {}).get('code_contract'), 'result_axis': summary['result_axis'],
            'code_axis': summary['code_axis'], 'combined_score': None,
            'cleanup_attestation': 'cleanup_attestation.json', 'judge_broker_stats': 'judge_broker_stats.json',
            'manual_freeze': manual_block(self.ctx)})
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg, 'judge_cleanup': receipt}


class Aider(Adapter):
    summary_incomplete = {'builder_integration_incomplete'}

    def _lower_image(self):
        ctx_file = self.ctx.lifecycle / 'product_attempts/context.json'
        image = read_json(ctx_file).get('runtime_image') if ctx_file.is_file() else None
        return image or 'agentswe/edit-candidate-python311:0826'

    def controller(self, endpoint=None):
        from harbor.agentloop_controller import Controller
        if self.ctx.kind == 'readiness':
            return Controller(self.ctx.lifecycle, self.fos.ROOT / 'input/repository', endpoint, True,
                              dependency_overlay=None, image=self._lower_image(), public_cases=('dev_001',),
                              hidden_cases=('test_001',), pilot_not_formal=True, max_dev_rounds=2,
                              readiness_profile=READINESS_PROFILE, current_binding=self.ctx.binding)
        return Controller(self.ctx.lifecycle, self.fos.ROOT / 'input/repository', endpoint, True,
                          dependency_overlay=None, image=self._lower_image(), max_dev_rounds=MAX_DEV_ROUNDS)

    def records(self):
        return read_json(self.ctx.lifecycle / 'dev_lifecycle.json').get('records') or []

    def frozen_on_disk(self):
        return read_json(self.ctx.lifecycle / 'dev_lifecycle.json').get('frozen')

    def latest_source(self, record):
        materialized = Path(record.get('materialized_path')
                            or self.ctx.lifecycle / f"materialized_{int(record['submission_number']):03d}")
        return materialized, None

    def digest(self, path):
        from harbor.agentloop_controller import digest
        return digest(path)

    def _attestation(self):
        p = self.ctx.run / 'builder_session_attestation.json'
        return read_json(p) if p.is_file() else {}

    def strict_native(self):
        return self._attestation().get('native_evidence') or {}

    def relaxed_native(self) -> tuple[dict, bool]:
        """verify_native(allow_interrupted=True) with the inputs _run_formal used (from the run's own files)."""
        from harbor.native_builder_evidence import verify_native
        att = self._attestation()
        records, product_bound = [], True
        for record in self.records():
            materialized = Path(record.get('materialized_path') or self.ctx.lifecycle
                                / f"materialized_{int(record['submission_number']):03d}") / 'aider'
            product_bound &= materialized.is_dir() and self.digest(materialized) == record.get('native_product_source_digest')
            records.append({**record, 'build': {'candidate_repo_digest': record.get('native_product_source_digest')}})
        events = att.get('events')
        if events is None and (self.ctx.run / 'builder_feedback_deliveries.json').is_file():
            events = read_json(self.ctx.run / 'builder_feedback_deliveries.json')  # readiness layout
        deliveries = [e for e in events or [] if e.get('event') == 'feedback_delivered']
        obs_path = self.ctx.run / 'builder_native_observations.json'
        observations = read_json(obs_path) if obs_path.is_file() else []
        return verify_native(self.ctx.run, records, deliveries, observations, allow_interrupted=True), product_bound

    def freeze_paths(self):
        return [self.ctx.lifecycle / 'frozen_candidate', self.ctx.lifecycle / 'freeze_manifest.json']

    def do_freeze(self, log):
        run = self.ctx.run
        ctrl = self.controller()
        frozen = ctrl.frozen
        if frozen is None:
            backup(self.ctx.lifecycle / 'dev_lifecycle.json', True, log)
            frozen = ctrl.freeze_latest('builder_exit')
            log.append('froze latest accepted Candidate via harbor.agentloop_controller.Controller.freeze_latest')
        else:
            log.append('formal path had already frozen the latest accepted Candidate (unconditional '
                       "freeze_latest('builder_exit')); verified, not re-frozen")
        relaxed, product_bound = self.relaxed_native()
        if not (relaxed.get('valid') and product_bound):
            raise Refusal('verify_native(allow_interrupted=True) did not pass: '
                          + json.dumps(relaxed.get('errors'))[:400] + f' product_bound={product_bound}')
        backup(run / 'builder_session_attestation.json', True, log)
        att = self._attestation()
        att['freeze'] = frozen
        att['same_session'] = True
        att['native_evidence_budget_interrupted'] = relaxed
        att['manual_freeze'] = manual_block(self.ctx, 'same_session set from verify_native(allow_interrupted=True): '
                                            'the relaxation _run_formal grants a max_dev_rounds interrupt; the '
                                            'strict proof, failing only on the missing terminal event, stays in '
                                            'native_evidence')
        write_json(run / 'builder_session_attestation.json', att)
        return frozen

    def hidden_paths(self):
        run = self.ctx.run
        return [run / 'brokers/hidden.cid', run / 'hidden_broker_initial.json',
                self.ctx.lifecycle / 'hidden-after-freeze-attestation.json', self.ctx.lifecycle / 'evaluations/hidden']

    def _suffix(self):
        return hashlib.sha256(str(self.ctx.run).encode()).hexdigest()[:10]

    def plan_hidden(self):
        return {'broker': 'aider-formal-hidden-' + self._suffix(),
                'script': str(self.fos.ROOT / 'evaluator/broker/lower_responses_broker.py'),
                'effort': self.fos.LOWER_EFFORT, 'cidfile': str(self.ctx.run / 'brokers/hidden.cid'),
                'lower_image': self._lower_image(),
                'runner': 'harbor.agentloop_controller.Controller.run_hidden() (test_001..test_006)'}

    def do_hidden(self, log):
        f, run = self.fos, self.ctx.run
        plan = self.plan_hidden()
        cid = Path(plan['cidfile'])
        port = free_port()
        endpoint = f'http://127.0.0.1:{port}/v1/responses'
        cleanup = None
        try:
            f.start_broker(name=plan['broker'], script=Path(plan['script']), credential=CREDENTIAL,
                           value_port=port, effort=f.LOWER_EFFORT, cidfile=cid)
            initial = f.stats(endpoint)
            if int((initial.get('runtime') or {}).get('calls', 0)) != 0:
                raise Refusal('formal hidden broker must start at zero calls')
            write_json(run / 'hidden_broker_initial.json', {'started_after_freeze': True, 'independent_from_public': True,
                                                            'stats': initial, 'manual_freeze': True})
            ctrl = self.controller(endpoint)
            if not ctrl.frozen:
                raise Refusal('controller state is not frozen')
            results = ctrl.run_hidden()
        finally:
            cleanup = f.cleanup_owned_containers({'hidden': plan['broker']}, {'hidden': cid.exists()}, {'hidden': cid})
        return {'hidden': results, 'broker_cleanup': cleanup,
                'attestation': read_json(self.ctx.lifecycle / 'hidden-after-freeze-attestation.json')}

    def finalize_plan(self, endpoint):
        return ['python3', str(self.fos.ROOT / 'evaluator/formal_finalize.py'), '--run-dir', str(self.ctx.run),
                '--credential-file', str(CREDENTIAL), '--result-judge-broker-endpoint', endpoint]

    def do_finalize(self, log):
        f, run = self.fos, self.ctx.run
        cid = self.ctx.evidence / 'result_judge.cid'
        name = 'aider-formal-result-judge-' + self._suffix() + '-mf'
        port = free_port()
        endpoint = f'http://127.0.0.1:{port}/v1/responses'
        done, cleanup = None, None
        try:
            f.start_broker(name=name, script=f.JUDGE_BROKER_SCRIPT, credential=CREDENTIAL, value_port=port,
                           effort=f.BUILDER_EFFORT, cidfile=cid)
            done = subprocess.run(self.finalize_plan(endpoint), text=True, capture_output=True, check=False)
            try:
                f.save_role_stats(self.ctx.evidence, 'result_judge', endpoint, 'direct')
            except Exception as exc:  # stats are diagnostic; the finalizer output is authoritative
                log.append(f'judge stats not saved: {type(exc).__name__}: {exc}')
        finally:
            cleanup = f.cleanup_owned_containers({'result_judge': name}, {'result_judge': cid.exists()}, {'result_judge': cid})
        write_log(self.ctx, done)
        out = run / 'formal_aggregation.json'
        agg = read_json(out) if out.is_file() else {'formal_result_publishable': False, 'result_axis': 'N/A',
                                                     'code_axis': 'N/A', 'reasons': [f'formal finalizer failed with exit {done.returncode if done else None}']}
        ctrl = self.controller()
        hidden_att = read_json(self.ctx.lifecycle / 'hidden-after-freeze-attestation.json')
        for name_ in ('summary.json', 'one_stop_summary.json'):
            backup(run / name_, True, log)
        att = self._attestation()
        write_json(run / 'summary.json', {
            'status': 'formal_result_ready' if agg.get('formal_result_publishable') else 'lifecycle_complete_result_not_publishable',
            'formal_result_claimed': agg.get('formal_result_publishable') is True,
            'code_score_claimed': agg.get('code_score_publishable') is True,
            'builder_session_id': att.get('builder_session_id'), 'records': ctrl.records, 'freeze': ctrl.frozen,
            'hidden': hidden_att.get('results'), 'formal_aggregation': agg,
            'finalizer_exit_code': done.returncode if done else None, 'manual_freeze': manual_block(self.ctx)})
        write_json(run / 'one_stop_summary.json', {
            'status': 'completed' if agg.get('formal_result_publishable') else 'formal_result_not_publishable',
            'dev_lifecycle': ctrl.records, 'freeze': ctrl.frozen,
            'hidden': {'inventory': list(HIDDEN6), 'summary': hidden_att.get('results')},
            'result_judge_contracts': agg.get('result_judge_contracts'), 'code_contract': agg.get('code_contract'),
            'combined_score': None, 'cleanup_attestation': str(run / 'builder_container_cleanup.json'),
            'manual_freeze': manual_block(self.ctx)})
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg, 'judge_cleanup': cleanup}


class DeepTutor(Adapter):
    summary_incomplete = {'formal_evidence_incomplete'}

    def controller(self, endpoint=None):
        """AcceptedSubmissionController keeps its records in memory only and its __init__ rewrites
        builder_session_opened.json, so it is rebuilt from lifecycle/controller_state.json and
        builder_session_opened.json without calling __init__; freeze() is then the tree's own code."""
        import agentloop.two_round_controller as trc
        from agentloop.protocol import DEV_CASES
        opened = read_json(self.ctx.lifecycle / 'builder_session_opened.json')
        state = read_json(self.ctx.lifecycle / 'controller_state.json')
        c = trc.AcceptedSubmissionController.__new__(trc.AcceptedSubmissionController)
        c.run_dir = self.ctx.lifecycle.resolve()
        c.session_id = opened['session_id']
        c.builder_witness = trc._validated_builder_witness(opened['builder_witness'], c.session_id)
        c.builder_witness_digest = trc.object_digest(c.builder_witness)
        if c.builder_witness_digest != opened.get('builder_witness_digest'):
            raise Refusal('builder witness digest does not reproduce')
        readiness = self.ctx.kind == 'readiness'
        c.dev_cases = ('dev_001',) if readiness else tuple(DEV_CASES)
        c.max_dev_rounds = 2 if readiness else MAX_DEV_ROUNDS
        c.pilot_not_formal = readiness
        c.readiness_profile = READINESS_PROFILE if readiness else None
        c.current_binding = self.ctx.binding if readiness else None
        c.records = state.get('records') or []
        c.frozen = state.get('frozen')
        c.feedback_record = state.get('latest_feedback')
        c.infrastructure_attempts = state.get('infrastructure_attempts', 0)
        if any(r.get('session_id') != c.session_id or r.get('builder_witness_digest') != c.builder_witness_digest
               for r in c.records):
            raise Refusal('accepted records are not bound to the rebuilt session witness')
        return c

    def records(self):
        return read_json(self.ctx.lifecycle / 'controller_state.json').get('records') or []

    def frozen_on_disk(self):
        return read_json(self.ctx.lifecycle / 'controller_state.json').get('frozen')

    def latest_source(self, record):
        return Path(str(record['candidate_path'])), record.get('candidate_digest')

    def digest(self, path):
        from agentloop.protocol import tree_digest
        return tree_digest(path)

    def strict_native(self):
        p = self.ctx.run / 'builder_native_attestation.json'
        return read_json(p) if p.is_file() else {}

    def lifecycle_ready_checks(self) -> dict:
        from agentloop.protocol import DEV_CASES
        recs = self.records()
        cases = ('dev_001',) if self.ctx.kind == 'readiness' else tuple(DEV_CASES)
        return {f'round {r.get("round")}: dev_results terminal for {",".join(cases)}':
                set(r.get('dev_results', {})) == set(cases) and all(i.get('terminal') is True for i in r['dev_results'].values())
                for r in recs}

    def freeze_paths(self):
        return [self.ctx.lifecycle / 'frozen_candidate', self.ctx.lifecycle / 'freeze_manifest.json']

    def do_freeze(self, log):
        failed = [k for k, v in self.lifecycle_ready_checks().items() if not v]
        if failed:
            raise Refusal('lifecycle gate (non-native part) fails: ' + '; '.join(failed))
        for path in (self.ctx.lifecycle / 'controller_state.json', self.ctx.lifecycle / 'builder_session_attestation.json'):
            backup(path, True, log)
        ctrl = self.controller()
        if ctrl.frozen is not None:
            raise Refusal('controller already frozen')
        frozen = ctrl.freeze()  # the formal path calls freeze() with no builder_exit_evidence
        att_path = self.ctx.run / 'builder_session_attestation.json'
        if att_path.is_file():
            backup(att_path, True, log)
            att = read_json(att_path)
        else:
            att = {'schema_version': 'agentswe-deeptutor-manual-freeze-run-attestation/v1'}
        att['freeze'] = frozen
        att['manual_freeze'] = manual_block(self.ctx, 'builder_process.json keeps lifecycle_ready_for_hidden=false '
                                            '(native[\'valid\'] is false); hidden runs under the budget-exhaustion rule')
        write_json(att_path, att)
        return frozen

    def hidden_paths(self):
        run = self.ctx.run
        return [run / 'hidden', run / 'hidden_broker.cid', run / 'hidden_broker_lifecycle.json']

    def plan_hidden(self):
        f = self.fos
        return {'broker': 'BrokerProcess(role=hidden, max_calls=12, max_tokens=240000)',
                'runtime_python': str(f.DEFAULT_RUNTIME_PYTHON),
                'runner': 'agentloop.run_hidden.run(lifecycle/freeze_manifest.json, <run>/hidden, case_ids=test_001..test_006)'}

    def do_hidden(self, log):
        f, run = self.fos, self.ctx.run
        broker, cleanup = None, None
        try:
            broker = f.BrokerProcess(run_dir=run, role='hidden', credential=CREDENTIAL, python=sys.executable,
                                     max_calls=12, max_tokens=240_000)
            hidden = f.run_hidden(self.ctx.lifecycle / 'freeze_manifest.json', run / 'hidden',
                                  broker_endpoint=broker.endpoint_local, python_executable=str(f.DEFAULT_RUNTIME_PYTHON.absolute()),
                                  case_ids=f.HIDDEN_CASES, pilot_not_formal=False)
        finally:
            cleanup = f.stop_brokers((broker,))
        return {'hidden': hidden, 'broker_cleanup': cleanup}

    def finalize_plan(self, endpoint):
        return [sys.executable, str(self.fos.ROOT / 'evaluator' / 'formal_finalize.py'), '--run-dir', str(self.ctx.run),
                '--credential-file', str(CREDENTIAL), '--result-judge-broker-endpoint', endpoint]

    def do_finalize(self, log):
        f, run = self.fos, self.ctx.run
        judge, done, cleanup = None, None, None
        try:
            judge = f.BrokerProcess(run_dir=self.ctx.evidence, role='result_judge', credential=CREDENTIAL,
                                    python=sys.executable, max_calls=40, max_tokens=3_000_000, broker_kind='responses_xhigh')
            done = subprocess.run(self.finalize_plan(judge.endpoint_local), text=True, capture_output=True, check=False)
        finally:
            cleanup = f.stop_brokers((judge,))
        write_log(self.ctx, done)
        out = run / 'formal_aggregation.json'
        agg = read_json(out) if out.is_file() else None
        hidden_att = run / 'hidden' / 'hidden-after-freeze-attestation.json'
        hidden_result = read_json(run / 'hidden' / 'hidden-result.json') if (run / 'hidden' / 'hidden-result.json').is_file() else {}
        for name_ in ('summary.json', 'one_stop_summary.json'):
            backup(run / name_, True, log)
        summary = read_json(run / 'summary.json') if (run / 'summary.json').is_file() else {}
        summary.update({
            'status': 'formal_evidence_complete' if hidden_result.get('formal_result_eligible') else 'formal_evidence_incomplete',
            'freeze_manifest': str(self.ctx.lifecycle / 'freeze_manifest.json'),
            'hidden_attestation': str(hidden_att) if hidden_att.is_file() else None,
            'formal_finalizer_exit': done.returncode if done else None,
            'formal_aggregation': 'formal_aggregation.json' if agg else None,
            'formal_result_claimed': bool(agg and agg.get('formal_result_publishable')),
            'code_score_claimed': bool(agg and agg.get('code_score_publishable')),
            'result_axis': agg.get('result_axis') if agg else 'N/A', 'code_axis': agg.get('code_axis') if agg else 'N/A',
            'combined_score': None, 'manual_freeze': manual_block(self.ctx)})
        write_json(run / 'summary.json', summary)
        write_json(run / 'one_stop_summary.json', summary)
        return {'finalizer_exit': done.returncode if done else None, 'aggregation': agg, 'judge_cleanup': cleanup}


ADAPTERS = {'aider': Aider, 'ai-scientist': AiScientist, 'deeptutor': DeepTutor}


# -------------------------------------------------------------------------------- helpers --
def backup(path: Path, apply: bool, log: list) -> None:
    path = Path(path)
    target = path.with_name(path.name + SUFFIX)
    if not path.exists():
        log.append(f'backup: {path.name} does not exist (created new)')
        return
    if target.exists():
        raise Refusal(f'backup already exists, refusing to overwrite: {target}')
    log.append(f'backup: {path} -> {target.name}')
    if apply:
        shutil.copy2(path, target)


_MANUAL = {}


def manual_block(ctx: Ctx, note: str | None = None) -> dict:
    block = {'reason': REASON, 'manual': True, 'tool': str(TOOL), 'tool_sha256': sha256_file(TOOL),
             'record': str(ctx.evidence / 'manual_freeze_record.json'), **_MANUAL}
    if note:
        block['note'] = note
    return block


def write_log(ctx: Ctx, done) -> None:
    if done is None:
        return
    ctx.evidence.mkdir(exist_ok=True)
    (ctx.evidence / 'finalizer.stdout.log').write_text(done.stdout or '')
    (ctx.evidence / 'finalizer.stderr.log').write_text(done.stderr or '')


def unit_state(unit: str) -> str:
    return subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip() or 'unknown'


def containers_mounting(run: Path) -> list:
    ids = subprocess.run(['docker', 'ps', '-aq', '--no-trunc'], capture_output=True, text=True).stdout.split()
    hits = []
    for cid in ids:
        out = subprocess.run(['docker', 'inspect', '--format', '{{.Name}} {{.State.Status}} {{range .Mounts}}{{.Source}},{{end}}', cid],
                             capture_output=True, text=True).stdout
        if str(run) in out:
            hits.append(out.strip()[:160])
    return hits


# ---------------------------------------------------------------------------------- gates --
def gates(ctx: Ctx, ad: Adapter, stage: str) -> tuple[dict, dict]:
    """Every precondition, as name -> bool; plus facts for the record."""
    g, facts = {}, {}
    run = ctx.run
    g['formal Lite cell (apply allowed)'] = ctx.formal
    facts['kind'], facts['unit'] = ctx.kind, ctx.unit
    state = unit_state(ctx.unit)
    facts['unit_state'] = state
    g['unit not running'] = state not in {'active', 'activating', 'deactivating', 'reloading'}
    # tree has not moved since launch
    sys.path.insert(0, str(CONTROL))
    from audit_readiness import tree_digest as audit_tree_digest
    current = audit_tree_digest(ctx.tree)
    facts['tree_digest_now'] = current
    if ctx.formal:
        launch = read_json(ctx.launch_record)
        facts['launch_sibling_digest'] = launch.get('sibling_digest')
        g['tree digest == launch record sibling digest'] = launch.get('sibling_digest') == current
        g['result_judge.py sha == launch record'] = launch.get('result_judge_sha256') == sha256_file(CONTROL / 'result_judge.py')
        g['launch record describes this run'] = launch.get('run_dir') == str(run)
    # the Builder ended at the budget, not by itself
    seg_path = run / 'builder_segment_receipt.json'
    seg = read_json(seg_path) if seg_path.is_file() else {}
    last = (seg.get('attempts') or [{}])[-1]
    exc = last.get('harbor_exception') or {}
    deadline = seg.get('builder_deadline_epoch')
    ended = last.get('ended_at_epoch')
    facts['segment_last_attempt'] = {k: last.get(k) for k in ('exit_reason', 'exit_code', 'segment_index')}
    facts['segment_exception'] = exc.get('exception_type')
    facts['seconds_before_deadline_at_end'] = (deadline - ended) if (deadline and ended) else None
    facts['resume_decision'] = {k: (last.get('decision') or {}).get(k) for k in ('resume', 'refusals', 'remaining_seconds')}
    g['Builder cut at the budget (infrastructure_cut; AgentTimeoutError or <=600 s before deadline)'] = bool(
        last.get('exit_reason') == 'infrastructure_cut'
        and (exc.get('exception_type') == 'AgentTimeoutError'
             or (facts['seconds_before_deadline_at_end'] is not None and facts['seconds_before_deadline_at_end'] <= 600))
        and (last.get('decision') or {}).get('resume') is False)
    # strict native evidence failed for exactly the missing terminal event
    strict = ad.strict_native()
    facts['strict_native_errors'] = strict.get('errors')
    g['strict native evidence fails only on the missing terminal event'] = strict.get('errors') == [NO_TERMINAL]
    # formal path stopped at its lifecycle gate
    summ = read_json(run / 'summary.json') if (run / 'summary.json').is_file() else {}
    facts['summary_status'] = summ.get('status')
    g[f'summary status in {sorted(ad.summary_incomplete)}'] = summ.get('status') in ad.summary_incomplete
    # accepted history
    recs = ad.records()
    facts['accepted_rounds'] = len(recs)
    g['1..5 accepted Candidates'] = 1 <= len(recs) <= MAX_DEV_ROUNDS
    digests = [r.get('candidate_digest') for r in recs]
    g['accepted delivery digests distinct'] = len(digests) == len(set(digests))
    if recs:
        source, expected = ad.latest_source(recs[-1])
        facts['latest_accepted'] = {'round': recs[-1].get('round', recs[-1].get('submission_number')),
                                    'candidate_digest': recs[-1].get('candidate_digest'), 'source': str(source)}
        g['latest accepted source present'] = source.is_dir()
        if source.is_dir():
            got = ad.digest(source)
            facts['latest_source_digest'] = got
            if expected:
                g['latest accepted source digest unchanged since acceptance'] = got == expected
    if isinstance(ad, DeepTutor):
        g.update(ad.lifecycle_ready_checks())
    frozen = ad.frozen_on_disk()
    facts['frozen_on_disk'] = bool(frozen)
    if isinstance(ad, Aider):
        g['freeze (formal path, unconditional) present'] = bool(frozen)
        if frozen and recs:
            g['freeze bound to latest accepted Candidate'] = (
                frozen.get('candidate_delivery_digest') == recs[-1].get('candidate_digest')
                and frozen.get('accepted_candidate_digests') == digests
                and (ctx.lifecycle / 'frozen_candidate').is_dir()
                and ad.digest(ctx.lifecycle / 'frozen_candidate') == frozen.get('candidate_digest'))
    elif stage == 'freeze':
        g['not yet frozen'] = not frozen and not any(p.exists() for p in ad.freeze_paths())
    else:
        g['frozen (freeze stage done)'] = bool(frozen)
    manual_done = (ctx.evidence / 'manual_freeze_record.json').is_file()
    if stage in ('hidden', 'finalize'):
        g['manual freeze record present'] = manual_done
    if stage in ('freeze', 'hidden'):
        g['no hidden evidence yet'] = not any(p.exists() for p in ad.hidden_paths())
    if stage == 'finalize':
        hidden_att = (ctx.lifecycle / 'hidden-after-freeze-attestation.json') if not isinstance(ad, DeepTutor) \
            else (run / 'hidden' / 'hidden-after-freeze-attestation.json')
        g['hidden stage done'] = hidden_att.is_file()
    g['no formal_aggregation.json yet'] = not (run / 'formal_aggregation.json').exists()
    hits = containers_mounting(run)
    facts['containers_mounting_run'] = hits
    g['no container mounts this run'] = not hits
    g['>= 5 GiB free'] = shutil.disk_usage(run).free >= 5 * 2**30
    g['credential file present'] = CREDENTIAL.is_file()
    return g, facts


# --------------------------------------------------------------------------------- stages --
def stage_check(ctx: Ctx, ad: Adapter, args, log: list) -> int:
    """Read-only: gates, controller reconstruction, latest-accepted vs existing freeze, plans."""
    before = snapshot(ctx.run)
    for stage in ('freeze', 'hidden', 'finalize'):
        g, facts = gates(ctx, ad, stage)
        log.append(f'-- gates for stage {stage}:')
        for name, ok in g.items():
            log.append(('     ok   ' if ok else '     FAIL ') + name)
        if stage == 'freeze':
            log.append('   facts: ' + json.dumps(facts, ensure_ascii=False)[:1500])
    try:
        ctrl = ad.controller()
        log.append(f'controller reconstructed from disk: {type(ctrl).__module__}.{type(ctrl).__name__}, '
                   f'records={len(ctrl.records)}, frozen={bool(ctrl.frozen)}')
        recs = ctrl.records
        if recs and ctrl.frozen:
            latest = recs[-1]
            f = ctrl.frozen
            same = (f.get('candidate_delivery_digest', f.get('candidate_digest')) == latest.get('candidate_digest')
                    or f.get('candidate_digest') == latest.get('candidate_digest'))
            log.append(f'existing freeze is the latest accepted Candidate: {same} '
                       f'(freeze source {f.get("source_submission")}, latest {latest.get("round", latest.get("submission_number"))})')
    except Exception as exc:
        log.append(f'controller reconstruction: {type(exc).__name__}: {exc}')
    if isinstance(ad, Aider):
        try:
            relaxed, bound = ad.relaxed_native()
            log.append(f'aider verify_native(allow_interrupted=True): valid={relaxed.get("valid")} '
                       f'errors={relaxed.get("errors")} product_bound={bound}')
        except Exception as exc:
            log.append(f'aider relaxed native check: {type(exc).__name__}: {exc}')
    log.append('hidden plan: ' + json.dumps(ad.plan_hidden()))
    log.append('finalize argv: ' + ' '.join(ad.finalize_plan('<fresh judge broker endpoint>')))
    after = snapshot(ctx.run)
    changed = sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})
    log.append(f'run directory untouched by this check: {not changed}' + (f' CHANGED: {changed[:5]}' if changed else ''))
    return 0 if not changed else 3


def run_stage(ctx: Ctx, ad: Adapter, args, log: list) -> int:
    record = ctx.evidence / 'manual_freeze_record.json'
    if args.stage != 'freeze' and record.is_file():
        prior = read_json(record)
        _MANUAL.update(operator=prior.get('operator'), operated_at=prior.get('operated_at'))
    g, facts = gates(ctx, ad, args.stage)
    failed = [name for name, ok in g.items() if not ok]
    for name, ok in g.items():
        log.append(('  ok   ' if ok else '  FAIL ') + name)
    if failed:
        raise Refusal('preconditions failed: ' + '; '.join(failed))
    if args.stage == 'freeze':
        log.append('would ' + ('verify the formal-path freeze and ' if isinstance(ad, Aider) else 'freeze the latest accepted '
                   'Candidate through the tree controller and ') + 'record the manual decision')
        if not args.apply:
            log.append('DRY-RUN: nothing written')
            return 0
        ctx.evidence.mkdir(exist_ok=True)
        _MANUAL.update(operator=args.operator, operated_at=now())
        frozen = ad.do_freeze(log)
        record = {'schema_version': 'agentswe-lite-manual-freeze/v1', 'task': ctx.task, 'run_dir': str(ctx.run),
                  'reason': REASON, 'manual': True, 'operator': args.operator, 'operated_at': _MANUAL['operated_at'],
                  'tool': str(TOOL), 'tool_sha256': sha256_file(TOOL),
                  'protocol_basis': 'Editing freeze rule: Builder exit OR budget exhaustion freezes '
                                    'the latest accepted Candidate; Harbor cut the session at the budget end before the '
                                    "formal path's native['valid'] gate let the freeze/hidden/finalize through.",
                  'not_done': ['Builder not restarted', 'Candidate bytes not modified', 'task tree / control plane not modified',
                               'no rebind'],
                  'freeze_manifest_freeze_reason': frozen.get('freeze_reason', 'n/a (deeptutor manifest has no freeze_reason)'),
                  'freeze': frozen, 'preconditions': g, 'facts': facts}
        write_json(ctx.evidence / 'manual_freeze_record.json', record)
        log.append(f'FROZEN: candidate_digest {frozen.get("candidate_digest")} source_submission {frozen.get("source_submission")}')
        return 0
    if args.stage == 'hidden':
        log.append('would start a fresh evaluator-owned hidden lower broker and run: ' + json.dumps(ad.plan_hidden()))
        if not args.apply:
            log.append('DRY-RUN: nothing started')
            return 0
        started = now()
        result = ad.do_hidden(log)
        write_json(ctx.evidence / 'hidden_stage_record.json', {'started_at': started, 'finished_at': now(),
                                                              'tool_sha256': sha256_file(TOOL), **result})
        log.append('hidden done: ' + json.dumps(result.get('attestation') or result.get('hidden'), ensure_ascii=False)[:1500])
        return 0
    if args.stage == 'finalize':
        log.append('would start a fresh Result-judge broker (deepseek-flash / max / 64000) and run: '
                   + ' '.join(ad.finalize_plan('<fresh judge broker endpoint>')))
        if not args.apply:
            log.append('DRY-RUN: nothing started')
            return 0
        started = now()
        result = ad.do_finalize(log)
        write_json(ctx.evidence / 'finalize_stage_record.json', {'started_at': started, 'finished_at': now(),
                                                                'tool_sha256': sha256_file(TOOL), **result})
        agg = result.get('aggregation') or {}
        log.append(json.dumps({'finalizer_exit': result.get('finalizer_exit'), 'publishable': agg.get('formal_result_publishable'),
                               'result_axis': agg.get('result_axis'), 'reasons': agg.get('reasons')}, ensure_ascii=False)[:2000])
        return 0 if result.get('finalizer_exit') == 0 else 2
    raise Refusal('unknown stage')


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--task', required=True, choices=sorted(TREES))
    p.add_argument('--run-dir', required=True, type=Path)
    p.add_argument('--stage', required=True, choices=('check', 'freeze', 'hidden', 'finalize'))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    p.add_argument('--operator', default='')
    args = p.parse_args()
    if args.stage != 'check' and not (args.dry_run or args.apply):
        p.error('--dry-run or --apply is required for freeze/hidden/finalize')
    if args.apply and args.stage == 'freeze' and not args.operator.strip():
        p.error('--operator is required with --apply --stage freeze')
    if os.geteuid() != 0:
        print('REFUSED: run as root'); return 1
    ctx = Ctx(args.task, args.run_dir)
    if not ctx.run.is_dir():
        print(f'REFUSED: no run dir {ctx.run}'); return 1
    if args.apply and not ctx.formal:
        print('REFUSED: --apply only for a formal Lite cell'); return 1
    os.chdir(ctx.tree)
    lock = Path(f'/tmp/agentswe-lite-manual-freeze-{args.task}-{ctx.run.name}.lock')
    with open(lock, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('REFUSED: another invocation holds the lock'); return 1
        log: list = []
        print(f'== lite manual freeze  task={args.task}  stage={args.stage}  '
              f'mode={"APPLY" if args.apply else "READ-ONLY"}  {now()}\n   run={ctx.run} kind={ctx.kind}', flush=True)
        try:
            load_unit_env(ctx, log)
            ad = ADAPTERS[args.task](ctx, load_fos(ctx))
            code = stage_check(ctx, ad, args, log) if args.stage == 'check' else run_stage(ctx, ad, args, log)
        except Refusal as exc:
            for line in log:
                print('   ' + line)
            print(f'REFUSED: {exc}')
            return 1
        for line in log:
            print('   ' + line)
        if args.apply:
            ctx.evidence.mkdir(exist_ok=True)
            entry = {'at': now(), 'task': args.task, 'run_dir': str(ctx.run), 'stage': args.stage, 'exit': code,
                     'operator': args.operator, 'tool_sha256': sha256_file(TOOL), 'log': log}
            with (ctx.evidence / 'tool_invocations.log').open('a') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            with APPLY_LOG.open('a') as fh:
                fh.write(json.dumps({k: entry[k] for k in ('at', 'task', 'run_dir', 'stage', 'exit', 'operator', 'tool_sha256')}) + '\n')
        print(f'== exit {code}')
        return code


if __name__ == '__main__':
    raise SystemExit(main())
