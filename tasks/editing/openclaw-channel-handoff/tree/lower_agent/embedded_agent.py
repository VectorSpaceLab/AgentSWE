"""Run the edited product's native reasoning outside the two fault targets.

This is an initial entry choice, never a retry/fallback of an unknown Gateway
run. Only the model selects task actions; this runner starts, waits and cleans
up. The three sandboxes share one resource scope and the enclosing case
envelope; this one also carries the agent's own 600s wall clock, anchored here.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import subprocess
import time

from evaluator.case_budget import AGENT_CLOCK_SECONDS
from lower_agent.entry_contract import PRODUCTION_ENTRY
from lower_agent.product_sandbox import ProductSandbox

# The product's own last line about why its loop stopped.  Read from the
# evaluator-owned log this class creates and hashes, reported as an observation
# and never used as a gate: the Candidate controls this log's wording.
STOP_REASON = re.compile(r"ended with stopReason=([A-Za-z_][A-Za-z0-9_]*)")


def command(cluster, message_file, session_key, deadline):
    remaining = math.floor(deadline - time.monotonic())
    if remaining < 1:
        raise TimeoutError('no case time remains for the native embedded Agent')
    if not isinstance(session_key, str) or not session_key.startswith('agent:main:'):
        raise ValueError('embedded entry requires the case-owned main session key')
    return [str(cluster.node), str(cluster.product / 'openclaw.mjs'),
            'agent', '--local', '--agent', 'main', '--session-key', session_key,
            '--message-file', str(message_file), '--channel', 'webchat',
            '--thinking', 'medium', '--timeout', str(remaining), '--json']


class EmbeddedAgent:
    def __init__(self, cluster, *, message, session_key):
        if not isinstance(message, str) or not message.strip():
            raise ValueError('native Agent task must be non-empty text')
        encoded = message.encode('utf-8')
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError('native message-file exceeds the product input limit')
        self.cluster = cluster
        # The published 600s is the AGENT's wall clock and it starts here: the
        # evaluator has already materialised the product and brought both
        # Gateways up (214.7-249.7s per case on the 0919 formal run) and that
        # setup has its own allowance.  The product's own node/OpenClaw boot is
        # inside this window because it is Candidate work.  The case envelope
        # still caps it, so an over-long setup shortens the clock instead of
        # overrunning the case.
        self.clock_started = time.monotonic()
        self.deadline = min(cluster.deadline, self.clock_started + AGENT_CLOCK_SECONDS)
        self.output = cluster.output / 'embedded-agent'
        self.output.mkdir(exist_ok=False)
        # An explicitly new file under the public workspace, not an oracle
        # mount, shell argument, or overwrite of a Candidate-authored result.
        self.message_file = cluster.workspace / 'agentswe-native-task.md'
        with self.message_file.open('x', encoding='utf-8') as stream:
            stream.write(message)
        self.argv = command(cluster, self.message_file, session_key, self.deadline)
        self.sandbox = ProductSandbox(product=cluster.product, state=cluster.state,
            workspace=cluster.workspace, runtime=cluster.runtime, output=self.output,
            endpoint=cluster.endpoint, bridge_port=cluster.bridge, env=cluster.env,
            deadline=self.deadline, product_readonly=cluster.product_readonly,
            case_socket=cluster.case_socket)
        # Deliberately no native service UDS/HTTP relays and no Gateway origin
        # registration: model tools must use the selected real Gateway client.
        self.process = None
        self.evidence = {'schema_version': 'openclaw-native-embedded-run-v1',
            'production_entry': PRODUCTION_ENTRY, 'session_key': session_key,
            'message_sha256': hashlib.sha256(encoded).hexdigest(),
            'command': self.argv, 'log': str(self.output / 'native-agent.log'),
            'deadline_monotonic': self.deadline, 'fallback_attempted': False,
            'clock_started_monotonic': self.clock_started,
            'agent_clock_seconds': AGENT_CLOCK_SECONDS,
            'agent_clock_full': self.deadline-self.clock_started >= AGENT_CLOCK_SECONDS,
            'observed_stop_reason': None,
            'evaluator_authored_result': False, 'status': 'not_started'}

    def start(self):
        if self.process is not None:
            raise RuntimeError('native Agent can be started only once per case')
        with (self.output / 'native-agent.log').open('x') as log:
            self.process = self.sandbox.start(self.argv, log,
                startup_deadline=min(self.deadline, time.monotonic() + 120))
        own = self.sandbox.namespace
        if any(own['net_inode'] == sandbox.namespace['net_inode']
               for sandbox in self.cluster.sandboxes.values()):
            raise RuntimeError('native reasoning must not share a fault-target namespace')
        self.evidence.update(status='running', started_monotonic=time.monotonic(),
            namespace=dict(own), wrapper_pid=self.process.pid,
            native_fixture_roles=sorted(self.sandbox.native_relays))
        return self.process

    def wait(self):
        if self.process is None:
            raise RuntimeError('native Agent has not started')
        try:
            code = self.process.wait(timeout=max(0, self.deadline - time.monotonic()))
            self.evidence.update(status='completed' if code == 0 else 'candidate_process_error',
                                 exit_code=code)
        except subprocess.TimeoutExpired:
            self.evidence.update(status='timeout', exit_code=None)
        self.evidence['ended_monotonic'] = time.monotonic()
        return dict(self.evidence)

    def close(self, seconds=2):
        # ``deadline`` is the product work cutoff.  The launcher reserves a
        # separate cleanup window after that cutoff and passes it here.  Do
        # not clamp that reserved budget against the already expired product
        # deadline: doing so turns a valid cleanup allowance into a zero
        # second join and can leave the owned creator thread unreaped.
        cleanup_seconds = max(0.0, float(seconds))
        self.evidence['sandbox'] = self.sandbox.close(cleanup_seconds)
        self.evidence['cleanup_complete'] = bool(
            self.evidence['sandbox'].get('gateway_wrapper_reaped'))
        path = self.output / 'native-agent.log'
        if path.is_file():
            raw = path.read_bytes()
            self.evidence['log_sha256'] = hashlib.sha256(raw).hexdigest()
            found = STOP_REASON.findall(raw[-65536:].decode('utf-8', 'replace'))
            self.evidence['observed_stop_reason'] = found[-1] if found else None
        return dict(self.evidence)
