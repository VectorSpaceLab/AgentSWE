#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
FORMAL_ROOT = Path("@@AGENTSWE_EDITING_RUNS@@/formal")
SMOKE_ROOT = Path("@@AGENTSWE_EDITING_RUNS@@/smoke")
CREDENTIAL_FILE = Path("@@AGENTSWE_CREDENTIAL_FILE@@")
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
AUTHORITATIVE_CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
RESULT_JUDGE = ROOT / "result_judge.py"
FORMAL_AXES_SHARED = ROOT / "formal_axes_shared.py"
BUILDER_BROKER_XHIGH = ROOT / "builder_broker_xhigh.py"
BUILDER_BROKER_RUNTIME = ROOT / "builder_broker_runtime.py"
BUILDER_REQUEST_LEDGER = ROOT / "builder_request_ledger.py"
RESPONSES_STREAM = ROOT / "responses_stream.py"
RESPONSES_BROKER_XHIGH = ROOT / "responses_broker_xhigh.py"
ONE_STOP_CONTRACT_SHARED = ROOT / "one_stop_contract_shared.py"
DEV_LIFECYCLE_SHARED = ROOT / "dev_lifecycle_shared.py"
JUDGE_BROKER_XHIGH = ROOT / "judge_broker_xhigh.py"
JUDGE_BROKER_RUNTIME = ROOT / "judge_broker_runtime.py"
# Control-plane Python only. Product/lower native runtimes remain task-owned.
CONTROL_PYTHON = Path('/usr/bin/python3')
ALIGNMENT_SNAPSHOT = ROOT / "create_alignment_snapshot.json"
# "release": the Create-alignment provenance (the paper's Creation files and selected run bundles) is not
# available in a release install; validate_formal_config records it as such instead of checking it, and the
# pre-repair snapshot binds each task to the release template tree (written by agentswe setup).
# "paper": the original checks, runnable only where the paper archives exist.
PROVENANCE_MODE = "@@AGENTSWE_PROVENANCE_MODE@@"
SELECTED_CREATE_PROVENANCE = Path("@@AGENTSWE_EDITING_STATE@@/create-selected-alignment-001/selected_alignment_audit.json")
CONFIGURATION_DELTA_REGISTRY = ROOT / "configuration_delta_registry.json"

PROFILES = {
    # codex_xhigh is the SELECTED slot (SELECTED_PROFILES, FORMAL_ROOT/codex_xhigh,
    # the ten-branch inventory).  Its contents, not its name, identify the cell.
    "codex_xhigh": {
        "builder_agent": "codex",
        "builder_model": "deepseek-flash",
        "builder_reasoning_effort": "max",
        "builder_harness_version": "0.144.1",
        "provider": "gateway-responses",
    },
    "codex_gpt55_xhigh": {
        "builder_agent": "codex",
        "builder_model": "gpt-5.5",
        "builder_reasoning_effort": "xhigh",
        "builder_harness_version": "0.144.1",
        "provider": "gateway-responses",
    },
    "codex_gpt56sol_xhigh": {
        "builder_agent": "codex",
        "builder_model": "gpt-5.6-sol",
        "builder_reasoning_effort": "xhigh",
        "builder_harness_version": "0.144.1",
        "provider": "gateway-responses",
    },
    "deepseek_max": {
        "builder_agent": "codex",
        "builder_model": "deepseek-v4-flash",
        "builder_reasoning_effort": "max",
        "builder_harness_version": "0.144.1",
        "provider": "deepseek-responses",
    },
    "deepseek_pro_max": {
        "builder_agent": "codex",
        "builder_model": "deepseek-v4-pro",
        "builder_reasoning_effort": "max",
        "builder_harness_version": "0.144.1",
        "provider": "deepseek-responses",
    },
}

TASKS = {
    "claude": Path("@@AGENTSWE_EDITING_TASKS@@/claude-policy-provenance/tree"),
    "aider": Path("@@AGENTSWE_EDITING_TASKS@@/aider-worktree-transaction/tree"),
    "openhands": Path("@@AGENTSWE_EDITING_TASKS@@/openhands-effect-recovery/tree"),
    "openclaw": Path("@@AGENTSWE_EDITING_TASKS@@/openclaw-channel-handoff/tree"),
    "codex": Path("@@AGENTSWE_EDITING_TASKS@@/codex-execution-residual/tree"),
    "ai-scientist": Path("@@AGENTSWE_EDITING_TASKS@@/ai-scientist-reproducibility-gate/tree"),
    "deepcode": Path("@@AGENTSWE_EDITING_TASKS@@/deepcode-claim-traceability/tree"),
    "deeptutor": Path("@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/tree"),
    "dyad": Path("@@AGENTSWE_EDITING_TASKS@@/dyad-acceptance-driven/tree"),
    "openwiki": Path("@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree"),
}

SELECTED_PROFILES = ("codex_xhigh",)
MAX_DEV_ROUNDS = 5
N_CONCURRENT = 1
DEV_PASSED_IS_AUTOMATIC_FREEZE = False

# N_CONCURRENT is per Harbor job/trial, not the campaign dispatch width.
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"
PHASE_CONCURRENCY = {"provider_free": 10, "builder_initial": 2, "builder_after_canary": 4,
                     "builder_resource_max": 8, "large_build": 2, "lower": 4,
                     "judge": 4, "registry_gate_writer": 1}
