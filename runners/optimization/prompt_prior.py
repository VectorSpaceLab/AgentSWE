"""Task-keyed evidence-based soft priors for the paper's Discussion experiment.

This module intentionally contains only the two approved treatment blocks.  It
does not contain historical run identifiers, scores, hidden results, candidate
artifacts, trajectories, or case-specific examples.  Selection is by the
benchmark contract id, never by a case or row id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


TAU3_PRIOR = """## Evidence-based soft prior

Treat this as a general design prior, not as a gold solution and not as permission to target individual development cases.

Start from the runnable starter and prefer the smallest policy-layer change that improves behavior. Do not rewrite `run_harness.py` unless a public-contract violation is demonstrated. Keep the policy compact and action-oriented rather than restating an exhaustive retail policy.

Use this control loop:

- authenticate before account or order access and maintain one consistent user identity;
- track every requested outcome and every unresolved field;
- read the relevant records before a write, and derive ownership, status, eligibility, identifiers, quantities, availability, payment and address constraints only from tool output;
- issue one schema-valid tool call per turn and never invent a missing value;
- before every state-changing action, including a default profile/address change, state the complete proposed mutation and obtain unambiguous confirmation;
- once confirmed, perform the one correct mutation with all collected fields, then report only what the tool result establishes;
- preserve duplicate item quantities and same-product replacement alignment; recover only with bounded, materially different, safe retries;
- finish concisely only after the complete request checklist is resolved.

Keep this task-agnostic: do not branch on case, row or task identifiers; do not add a worked example tailored to a development case; and do not hard-code an exhaustive catalogue of tools, statuses or amount combinations.
"""


PINCHBENCH_PRIOR = """## Evidence-based soft prior

Treat the existing starter as a strong baseline. Optimize for cross-task robustness with the smallest general, independently testable change. Do not reproduce verbose category playbooks or keyword routing: those patterns improved public development scores historically but did not generalize.

Keep the policy compact and task-agnostic. For each request:

- identify the exact deliverable, path, format, content and constraints;
- inspect only the relevant workspace inputs before acting;
- create the requested artifact with the smallest reliable task-appropriate tool sequence while preserving unrelated data;
- inspect the mutation result and perform outcome-level verification: existence, exact name and format, parseability, requested content, and focused tests or recomputation;
- on failure, diagnose the exact error, retry only transient failures, use a bounded equivalent fallback, and report an unverifiable limitation honestly;
- act early enough to leave budget for read-back and repair.

Do not add category enumerations, keyword routers, case-specific branches, blanket full-tree scans, blanket tool choices, or ever-growing checklists. Unless a local public-contract test exposes a real structural bug, preserve `run_harness.py` and make one small, reversible policy change at a time.
"""


@dataclass(frozen=True)
class Prior:
    task: str
    source: str
    text: str
    injection_position: str = "after_original_builder_instruction"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def metadata(self) -> dict[str, str | int]:
        return {
            "task": self.task,
            "source": self.source,
            "sha256": self.sha256,
            "character_count": len(self.text),
            "injection_position": self.injection_position,
            "treatment": "prior" if self.text else "none",
        }


def prior_for_benchmark(benchmark_id: str) -> Prior:
    # AgentSWE-Lite: no prior (main-run protocol). Keep the old body unreachable for provenance.
    if benchmark_id in {"tau3-tool-agent-optimization-v1", "pinchbench-openclaw-agent-optimization-v1"}:
        return Prior(benchmark_id.split("-")[0], "lite:none", "")
    if benchmark_id == "tau3-tool-agent-optimization-v1":
        return Prior("tau3", "prompt_prior.py:TAU3_PRIOR", TAU3_PRIOR)
    if benchmark_id == "pinchbench-openclaw-agent-optimization-v1":
        return Prior("pinchbench", "prompt_prior.py:PINCHBENCH_PRIOR", PINCHBENCH_PRIOR)
    # Main-run protocol for the non-Lite Optimization tasks: no prior; the Builder instruction is then
    # byte-identical to the paper main-run one_stop builder_instruction().
    if benchmark_id in {"browsecomp-search-agent-optimization-v1", "terminalbench-code-agent-optimization-v1",
                        "osworld-desktop-agent-optimization-v1"}:
        return Prior(benchmark_id.split("-")[0], "main:none", "")
    raise ValueError(f"prompt-prior experiment does not support benchmark {benchmark_id!r}")

