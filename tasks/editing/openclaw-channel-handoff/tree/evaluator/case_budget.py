"""One case envelope: an evaluator setup allowance, then the agent's 600s clock.

The published 600 seconds are the lower AGENT's wall clock.  They start when the
agent's own sandbox is created, after the evaluator has materialised the product
and brought both Gateways up.  On the 0919 formal run that preparation cost
214.7s, 217.8s, 220.6s, 232.4s, 236.1s and 249.7s for test_001..test_006
(hidden/test_00N/run_report.json#timing_contract, native_agent.started_monotonic
against absolute_case_deadline_monotonic-600), i.e. 36-42 % of the old envelope.
It is evaluator work, not product work, so it now has its own allowance and is
not charged to the agent: three of the six hidden cases were cut off by the
budget guard 44.8-55.3s short of the wire after 39-50 real model turns.

The product cutoff is still 30 seconds before the case deadline: ten for
owned-process cleanup, then twenty for stopped-product evidence/publication.
The agent's own product boot (node, OpenClaw, session attach) stays inside the
agent clock because it is Candidate work.  This grants no extra model time
inside the agent's clock; it stops spending that clock on evaluator setup.
"""
from dataclasses import dataclass
import math
import time

AGENT_CLOCK_SECONDS = 600
# Evaluator-owned setup: product materialisation, two Gateway starts, native
# fixtures and health.  Ceiling, not a grant -- the case ends as soon as the
# agent is finished.  Measured maximum 249.7s; 300 leaves 20 % headroom.
SETUP_ALLOWANCE_SECONDS = 300
PRODUCT_CLEANUP_SECONDS = 10
FINALIZATION_SECONDS = 20
REPORT_SECONDS = 2
CASE_SECONDS = (SETUP_ALLOWANCE_SECONDS + AGENT_CLOCK_SECONDS
                + PRODUCT_CLEANUP_SECONDS + FINALIZATION_SECONDS)


@dataclass(frozen=True)
class CaseBudget:
    started: float
    deadline: float

    def __post_init__(self):
        if (not math.isfinite(self.started) or not math.isfinite(self.deadline)
                or not 0 < self.deadline-self.started <= CASE_SECONDS):
            raise ValueError('case envelope must be finite and at most930s')

    @property
    def launcher_deadline(self):
        return self.deadline-FINALIZATION_SECONDS

    @property
    def product_deadline(self):
        return self.launcher_deadline-PRODUCT_CLEANUP_SECONDS

    @property
    def evidence_deadline(self):
        return self.deadline-REPORT_SECONDS

    def remaining_for_launcher(self):
        current=time.monotonic()
        remaining=self.launcher_deadline-current
        if current>=self.product_deadline:
            raise TimeoutError('case preparation consumed product time; finalization is not borrowed')
        return remaining

    def snapshot(self):
        return {'schema_version':'openclaw-case-finalization-budget/v1',
            'started_monotonic':self.started,'case_deadline_monotonic':self.deadline,
            'product_deadline_monotonic':self.product_deadline,
            'launcher_deadline_monotonic':self.launcher_deadline,
            'evidence_deadline_monotonic':self.evidence_deadline,
            'maximum_case_seconds':CASE_SECONDS,
            'agent_clock_seconds':AGENT_CLOCK_SECONDS,
            'setup_allowance_seconds':SETUP_ALLOWANCE_SECONDS,
            'agent_clock_anchor':'lower agent sandbox creation, after evaluator setup',
            'product_cleanup_reserved_seconds':PRODUCT_CLEANUP_SECONDS,
            'post_launcher_finalization_reserved_seconds':FINALIZATION_SECONDS,
            'report_reserved_inside_finalization_seconds':REPORT_SECONDS,
            'extra_model_or_case_seconds':0}
