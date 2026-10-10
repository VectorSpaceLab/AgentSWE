"""Run the unmodified native Builder with direct auth and observed resources."""
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from agentloop.protocol import write_json
from harbor import direct_harbor_builder as direct
from harbor.builder_resources import BuilderResourceObserver


SHARED_BUILDER_SEGMENTS = "@@AGENTSWE_EDITING_CONTROL@@"


def builder_segments_runtime():
    """The shared Builder segment loop (package 97, builder_segments.py).

    One codex session may span more than one Harbor trial when an evaluator-side
    infrastructure cut ends a container mid-turn; that module owns the whole
    decision, the relaunch and <run>/builder_segments.json, so that all ten
    trees behave identically.  A run that never resumes is unaffected.
    """
    import sys as _sys
    if SHARED_BUILDER_SEGMENTS not in _sys.path:
        _sys.path.insert(0, SHARED_BUILDER_SEGMENTS)
    import builder_segments
    return builder_segments


def run_native_builder(*, lifecycle, config, credential, harbor, timeout=28920,
                       proxy='http://127.0.0.1:7890', provider_url='https://api.deepseek.com/v1'):
    run = lifecycle.run_dir
    composition = run / 'builder_task/environment/docker-compose.yaml'
    child = None
    observer = BuilderResourceObserver(run)
    observer.start()
    try:
        proxy_context = direct.existing_proxy_for_builder(config, composition, proxy,
            provider_url=provider_url) if proxy else nullcontext({'proxy_used': False})
        with proxy_context as proxy_receipt:
            write_json(run / 'builder_proxy.json', proxy_receipt)
            with direct.direct_auth(config, credential) as auth_receipt:
                write_json(run / 'builder_transport.json', {**auth_receipt,
                    'runtime_sha256': hashlib.sha256(Path(direct.__file__).read_bytes()).hexdigest(),
                    'native_model': 'deepseek-flash', 'native_effort': 'max',
                    'outer_timeout_seconds': timeout})
                # 97: one codex session, up to RESUME_CAP + 1 Harbor jobs.
                # The Popen + outer-deadline wait that used to be inlined here
                # is builder_segments.launch_harbor_job, called once per
                # segment and unchanged; run_segments only decides whether an
                # abnormally ended segment gets resumed inside this same run,
                # inside these same proxy / auth / socket contexts, on what is
                # left of this same budget, and writes the segment ledger.
                def adopt_builder_child(process):
                    nonlocal child
                    child = process
                with builder_segments_runtime().builder_session(
                        run, config, harbor=harbor, observer=observer,
                        write_json=write_json, on_start=adopt_builder_child) as session:
                    code = session.run(budget_seconds=timeout)
                    if not observer.finish()['valid']:
                        code = 125
                    return subprocess.CompletedProcess(child.args, code)
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
        observer.finish()
        readiness = (run / 'readiness_current_binding.json').is_file()
        cleanup = observer.cleanup_owned(process_started=child is not None,
                                         defer_removal=readiness)
        write_json(run / 'builder_native_stats.json', direct.native_stats(run))
        # Under readiness the evaluator keeps its own resources until the terminal
        # coordinator records cleanup, so ``complete`` is deliberately False and
        # terminal retention is what has to be proven in its place.
        proven = ((cleanup.get('retained_terminal') or cleanup.get('nothing_started'))
                  if readiness else cleanup['complete'])
        if not proven:
            raise RuntimeError('Native Builder container cleanup is unproven')
