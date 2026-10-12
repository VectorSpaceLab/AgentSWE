#!/usr/bin/env python3
"""Run native OpenClaw reasoning plus two real Gateways for one case."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lower_agent.runtime_resolver import resolve, write_manifest  # noqa: E402
from lower_agent.product_sandbox import ProductSandbox  # noqa: E402
from lower_agent.entry_contract import PRODUCTION_ENTRY  # noqa: E402
from lower_agent import sandbox_transport  # noqa: E402
from evaluator.case_service import PrivateFacts, make_private_facts  # noqa: E402
from evaluator.case_budget import (  # noqa: E402
    AGENT_CLOCK_SECONDS, CASE_SECONDS, FINALIZATION_SECONDS, SETUP_ALLOWANCE_SECONDS)
from controller.two_round_controller import tree_digest  # noqa: E402

MODEL = "deepseek-flash"
EFFORT = "high"
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
# The published 600s is the lower AGENT's wall clock (AGENT_CLOCK_SECONDS) and is
# anchored where the agent's own sandbox is created, in lower_agent/embedded_agent.py.
# CASE_TIMEOUT_SECONDS is the whole case envelope: the evaluator's setup allowance,
# that agent clock, and the cleanup/finalization reserve.  Setup -- materialising the
# product and starting both Gateways -- cost 214.7-249.7s per case on the 0919 formal
# run and is no longer charged to the agent.
CASE_TIMEOUT_SECONDS = CASE_SECONDS
GATEWAY_STARTUP_SECONDS = 120
HEALTH_RPC_SECONDS = 15
GATEWAY_CLEANUP_SECONDS = 10


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def broker_stats_url(endpoint: str) -> str:
    """Derive the evaluator-only stats route without exposing any credential."""
    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid broker endpoint: {endpoint!r}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/stats", "", ""))


def read_broker_stats(endpoint: str, *, timeout: float = 5) -> dict[str, object]:
    request = urllib.request.Request(
        broker_stats_url(endpoint),
        headers={"Authorization": f"Bearer {STATS_TOKEN}"},
    )
    if not math.isfinite(timeout) or timeout<=0:
        raise TimeoutError('no budget remains for broker statistics')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats response must be an object")
    return value


def runtime_stats(value: dict[str, object] | None) -> dict[str, int]:
    """Normalize both broker stats layouts used by the evaluator.

    The current broker exposes split token counters and ``successful_calls``;
    the earlier lifecycle broker exposes the compact
    ``runtime.calls``/``runtime.failures``/``runtime.tokens`` shape.  Hidden
    evidence must be comparable across either layout, so ``tokens`` is treated
    as the total-token alias and successful calls are derived when omitted.
    """
    runtime = value.get("runtime") if isinstance(value, dict) else None
    runtime = runtime if isinstance(runtime, dict) else {}

    def integer(key: str, default: int = 0) -> int:
        raw = runtime.get(key, default)
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return default

    calls = integer("calls")
    failures = integer("failures")
    successful_calls = integer("successful_calls", max(0, calls - failures))
    client_failures = integer("client_failures")
    provider_failures = integer("provider_failures")
    input_tokens = integer("input_tokens")
    output_tokens = integer("output_tokens")
    compact_tokens = integer("tokens")
    total_tokens = integer("total_tokens", compact_tokens or input_tokens + output_tokens)
    if integer('usage_unknown_calls'):
        input_tokens=output_tokens=total_tokens=None
    # Keep the compact alias in the normalized record as well.  This makes the
    # exact runtime.tokens source auditable without changing the result schema.
    return {
        "calls": calls,
        "failures": failures,
        "successful_calls": successful_calls,
        "client_failures": client_failures,
        "provider_failures": provider_failures,
        "usage_unknown_calls": integer("usage_unknown_calls"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tokens": total_tokens,
    }


# --- D13 (2026-09-19) recovered upstream transport failures ---------------------------
# An upstream transport failure the lower agent recovered from -- it issued a new logical
# request and a later one in the same ledger succeeded -- is infrastructure noise, not a
# provider failure for this case. Same allowlist and exclusions as the shared admission
# normalizers (harbor/0905-edit-case-repair/v2_usage_normalizers.py, transport_error);
# duplicated here because the control plane is not importable at run time.
_D13_TRANSPORT_TOKENS = ("brokenpipe", "connectionreset", "connectionaborted", "connectionclosed",
                         "remotedisconnected", "serverdisconnected", "incompleteread",
                         "chunkedencoding", "ssleof", "prematureclose")
_D13_PROVIDER_SIDE_TOKENS = ("readtimeout", "readtimedout", "sockettimeout", "timeouterror",
                             "timedout", "connecttimeout", "connectionerror")
_D13_NON_TRANSPORT_TOKENS = ("credential", "apikey", "unauthor", "forbidden", "invalidrequest",
                             "protocolfailure", "schema", "casedeadline", "deadlineexceeded",
                             "maxoutputtokens", "brokerrestart", "notdispatched", "notsent", "cancel")
_D13_NON_TRANSPORT_TEXT = ("client:", "client_", "client failure", "client error", "clientfailure")


def _d13_has_5xx(text):
    groups = "".join(character if character.isdigit() else " " for character in text).split()
    return any(len(group) == 3 and group[0] == "5" for group in groups)


def _d13_transport_error(error):
    if not isinstance(error, str) or not error.strip():
        return False
    text = error.lower()
    squeezed = "".join(character for character in text if character.isalnum())
    if any(token in squeezed for token in _D13_NON_TRANSPORT_TOKENS):
        return False
    if any(token in text for token in _D13_NON_TRANSPORT_TEXT):
        return False
    if any(token in squeezed for token in _D13_TRANSPORT_TOKENS):
        return True
    provider_side = any(marker in squeezed for marker in ("provider", "upstream", "http"))
    if provider_side and any(token in squeezed for token in _D13_PROVIDER_SIDE_TOKENS):
        return True
    return bool(provider_side and _d13_has_5xx(text))


def _d13_rows(stats):
    """Ledger rows of either family: intent rows (`requests`) or attempts (`attempts`)."""
    if not isinstance(stats, dict):
        return []
    for key in ("requests", "attempts"):
        rows = stats.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _d13_row_ok(row):
    if "model_response_available" in row or "usage_unknown" in row:
        return (row.get("state") == "terminal" and row.get("model_response_available") is True
                and row.get("usage_unknown") is False)
    return (row.get("ok") is True and row.get("usage_state") == "known"
            and row.get("upstream_completion") == "completed")


def _d13_row_error(row):
    if row.get("failure_kind") == "client" or row.get("provider_outcome") == "not_dispatched":
        return None
    for key in ("error", "transport_abort_reason", "error_type", "failure_reason"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _d13_row_attempts(row):
    for key in ("transport_attempts", "upstream_attempts"):
        if key in row:
            return row.get(key)
    return None


def _d13_identity(row):
    return row.get("request_sha256") or row.get("request_id")


def _d13_recovered_transport_calls(before, after):
    """Tolerated rows added between the snapshots; before=None counts the whole ledger."""
    rows = _d13_rows(after)
    seen = {_d13_identity(row) for row in _d13_rows(before)} if before is not None else set()
    count = 0
    for index, row in enumerate(rows):
        if _d13_identity(row) in seen or _d13_row_ok(row):
            continue
        if not _d13_transport_error(_d13_row_error(row)) or _d13_row_attempts(row) != 1:
            continue
        if any(_d13_row_ok(later) for later in rows[index + 1:]):
            count += 1
    return count
# --- end D13 --------------------------------------------------------------------------


# --- deadline-abort attribution (D13, 2026-09-19) -------------------------------------
# An attempt the evaluator's own broker killed at the absolute case deadline is an
# evaluator-initiated truncation: the Candidate did not finish inside the case budget.
# That is a Candidate outcome, never an infrastructure void. The attempt's token usage
# stays unknown and nothing is fabricated; only the attribution changes.
# broker/responses_broker.py is the only writer of this abort reason, so the attribution
# is never ambiguous, and D13's recovered-transport allowlist excludes 'casedeadline'
# outright, so the two carve-outs never overlap.
DEADLINE_ABORT_REASON = 'absolute_case_deadline'


def deadline_aborted(event):
    return isinstance(event, dict) and event.get('transport_abort_reason') == DEADLINE_ABORT_REASON


def _attempt_failed(event):
    """Mirror the broker snapshot's own 'failed' predicate."""
    return event.get('state') == 'terminal' and not event.get('model_response_available')
# --- end deadline-abort attribution ---------------------------------------------------


def broker_stats_delta(before: dict[str, object] | None, after: dict[str, object] | None) -> dict[str, int]:
    left, right = runtime_stats(before), runtime_stats(after)
    value={key: (None if right[key] is None or left[key] is None else max(0,right[key]-left[key])) for key in left}
    # D13: recovered upstream transport failures are counted apart and contribute no tokens.
    value['recovered_transport_calls']=_d13_recovered_transport_calls(before, after)
    if isinstance(before,dict) and isinstance(after,dict) and isinstance(before.get('attempts'),list) and isinstance(after.get('attempts'),list):
        old={event.get('request_id') for event in before['attempts']}
        current=[event for event in after['attempts'] if event.get('request_id') not in old]
        unknown=sum(bool(event.get('usage_unknown',True)) for event in current)
        for key in ('input_tokens','output_tokens','total_tokens'):
            value[key]=None if unknown>value['recovered_transport_calls'] else sum((event.get('usage') or {}).get(key,0) for event in current)
        value['tokens']=value['total_tokens']
        # Publish the deadline-abort split alongside the raw counters, which stay exactly
        # as the broker recorded them. The token rule above is D13's and is untouched: a
        # deadline abort is not a recovered transport row, so tokens stay None.
        value['deadline_aborted_calls']=sum(1 for event in current if deadline_aborted(event))
        value['unattributed_usage_unknown_calls']=sum(
            1 for event in current
            if event.get('usage_unknown',True) and not deadline_aborted(event))
        value['unattributed_failures']=sum(
            1 for event in current if _attempt_failed(event) and not deadline_aborted(event))
        value['unattributed_provider_failures']=sum(
            1 for event in current if _attempt_failed(event)
            and event.get('failure_kind')!='client' and not deadline_aborted(event))
    return value


def validate_agent_result(path: Path, case_id: str, private_values: list[str] | None = None) -> tuple[bool, str]:
    """Validate only the public terminal artifact contract.

    The evaluator never writes this artifact.  This function merely checks a
    file authored by the real OpenClaw agent and rejects evaluator-only values
    if a case-local private fact was accidentally copied into it.
    """
    if not path.is_file():
        return False, "agent_result.json is missing"
    if not path.resolve().is_relative_to(path.parent.resolve()):
        return False, "agent_result.json resolves outside the Agent artifact workspace"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"agent_result.json is not valid JSON: {type(exc).__name__}"
    if not isinstance(value, dict):
        return False, "agent_result.json must be an object"
    required = {"schema_version", "case_id", "decision", "observed_actions", "evidence", "uncertainty"}
    # The runtime delivery contract this launcher itself prints (:668-680) and every
    # test_cases/*/input.md + dev_cases/*/input.md REQUIRE decision_state, so it is an
    # accepted field.  Kept OUT of `required` so artifacts written before this change
    # stay valid, and no value check is added: nothing valid today becomes invalid.
    allowed = required | {"prohibited_disclosures", "decision_state"}
    missing = sorted(required - value.keys())
    if missing:
        return False, f"agent_result.json missing fields: {missing}"
    extra = sorted(set(value) - allowed)
    if extra:
        return False, f"agent_result.json has unsupported fields: {extra}"
    if value.get("schema_version") != "openclaw-agent-result-v1":
        return False, "agent_result.json has an unsupported schema_version"
    if value.get("case_id") != case_id:
        return False, "agent_result.json case_id does not match the runtime case"
    if not isinstance(value.get("decision"), str) or not value["decision"].strip():
        return False, "agent_result.json decision must be a non-empty string"
    if not isinstance(value.get("observed_actions"), list) or not all(isinstance(item, str) for item in value["observed_actions"]):
        return False, "agent_result.json observed_actions must be an array of strings"
    if not isinstance(value.get("uncertainty"), list) or not all(isinstance(item, str) for item in value["uncertainty"]):
        return False, "agent_result.json uncertainty must be an array of strings"
    if not isinstance(value.get("evidence"), list) or not all(isinstance(item, dict) and isinstance(item.get("kind"), str) and isinstance(item.get("reference"), str) for item in value["evidence"]):
        return False, "agent_result.json arrays have invalid types"
    serialized = json.dumps(value, ensure_ascii=False)
    for secret in private_values or []:
        if secret and secret in serialized:
            return False, "agent_result.json contains an evaluator-private value"
    return True, "valid"


def validate_agent_run_report(path: Path, private_values: list[str] | None = None) -> tuple[bool, str]:
    if not path.is_file():
        return False, "run_report.json is missing"
    if not path.resolve().is_relative_to(path.parent.resolve()):
        return False, "run_report.json resolves outside the Agent artifact workspace"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"run_report.json is not valid JSON: {type(exc).__name__}"
    if not isinstance(value, dict):
        return False, "run_report.json must be an object"
    if not isinstance(value.get("status"), str) or not value["status"].strip():
        return False, "run_report.json status must be a non-empty string"
    if not isinstance(value.get("commands"), list):
        return False, "run_report.json commands must be an array"
    if not isinstance(value.get("errors"), list):
        return False, "run_report.json errors must be an array"
    serialized = json.dumps(value, ensure_ascii=False)
    for secret in private_values or []:
        if secret and secret in serialized:
            return False, "run_report.json contains an evaluator-private value"
    return True, "valid"


def make_materialized_tree_writable(root: Path) -> None:
    """Make only the disposable product copy writable.

    A frozen Candidate is intentionally read-only.  ``shutil.copytree``
    preserves source directory modes, so a copy made from that tree would
    otherwise reject the runtime's subsequent ``node_modules``/``dist``
    injection.  Never call this on the frozen source itself.
    """
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
            path.chmod(mode | (0o200 if path.is_file() else 0o700))
        except OSError:
            # The later dependency-copy operation will report the concrete
            # materialization error if a disposable path remains unwritable.
            continue
    try:
        root.chmod(root.stat().st_mode | 0o700)
    except OSError:
        pass


def classify_execution(
    report: dict[str, object],
    delta: dict[str, int],
    artifact_valid: bool,
    artifact_error: str | None,
) -> tuple[str, str]:
    errors = " ".join(str(item) for item in report.get("errors", []))
    lower_status = str(report.get("status", "blocked"))
    # Unknown-usage rows that are neither deadline-aborted (excluded on the left) nor a
    # recovered transport failure (subtracted on the right) are still infrastructure.
    if (delta.get('unattributed_usage_unknown_calls', delta.get('usage_unknown_calls', 0))
            > delta.get('recovered_transport_calls', 0)):
        return "broker_infrastructure_error", "a case-local model request ended with unresolved provider usage/outcome; no automatic replay"
    for field in ('broker_in_flight_before', 'broker_in_flight_after'):
        if isinstance(report.get(field), int) and report[field] > 0:
            return "broker_infrastructure_error", "lower broker has an unresolved in-flight request at a case boundary"
    if lower_status == "blocked" or report.get("health") is None:
        return "launcher_infrastructure_error", "OpenClaw Gateway did not reach its production health entry"
    # A failure the evaluator's own deadline caused is a Candidate outcome, so a case whose
    # only failure is that truncation is not an all-calls-failed outage and not a
    # provider-path fault. A recovered transport failure always has a later success in the
    # same ledger, so the all-failed gate below cannot be reached by one.
    unattributed_failures = delta.get('unattributed_failures', delta['failures'])
    unattributed_provider_failures = delta.get('unattributed_provider_failures',
                                               delta.get('provider_failures', 0))
    known_client_only = (unattributed_failures > 0
                         and delta.get('client_failures') == unattributed_failures
                         and unattributed_provider_failures == 0)
    if unattributed_failures and not delta["successful_calls"] and not known_client_only:
        return "broker_infrastructure_error", "all evaluator-owned broker calls failed before a model result"
    provider_markers = ("broker", "provider", "FailoverError", "Responses", "502", "503", "504")
    if (any(marker.lower() in errors.lower() for marker in provider_markers)
            and unattributed_failures > delta.get('recovered_transport_calls', 0) and not known_client_only):
        return "broker_infrastructure_error", "OpenClaw reached the Gateway but the evaluator/provider path had a provider failure"
    if not artifact_valid:
        return "candidate_product_failure", artifact_error or "candidate did not produce a valid terminal artifact"
    if lower_status not in {"completed", "partial"}:
        return "candidate_behavior_failure", "candidate artifact exists but the product run did not complete normally"
    return "candidate_behavior_observed", "real OpenClaw run produced a valid case-bound terminal artifact"


def write_config(state: Path, workspace: Path, broker: str, token: str, model: str = MODEL) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    broker_base = broker.removesuffix("/responses")
    config = {
        # Hidden/public evaluation enters through the production Gateway RPC,
        # not through the browser dashboard.  Source/runtime Candidates do not
        # necessarily include the generated dist/control-ui bundle; leaving
        # the dashboard enabled makes OpenClaw start an unrelated pnpm UI
        # build, which can exhaust the evaluator memory budget before the
        # first broker call.  OpenClaw officially supports disabling this
        # surface while keeping the Gateway RPC fully enabled.
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "auth": {"mode": "token", "token": token},
            "controlUi": {"enabled": False},
        },
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "skipBootstrap": True,
                # Case-owned foreground reasoning only. Two shared-state
                # Gateways must not start unrelated main-session heartbeats.
                "heartbeat": {"every": "0m"},
                "timeoutSeconds": AGENT_CLOCK_SECONDS,
                "model": {"primary": f"broker/{model}"},
            },
            "entries": {"main": {"default": True}},
        },
        # The handoff benchmark exercises core Gateway methods and the core
        # embedded agent. Bundled optional plugins add substantial startup and
        # skill-discovery load but are not part of the evaluated product seam.
        "plugins": {"enabled": False},
        "cron": {"enabled": False},
        "models": {
            "mode": "merge",
            "providers": {
                "broker": {
                    "baseUrl": broker_base,
                    "api": "openai-responses",
                    "apiKey": PLACEHOLDER,
                    "models": [{"id": model, "name": model, "api": "openai-responses", "reasoning": True, "input": ["text"], "contextWindow": 128000, "maxTokens": 8192}],
                }
            },
        },
    }
    path = state / "openclaw.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def env_for(state: Path, config: Path, broker: str, runtime: dict[str, object]) -> dict[str, str]:
    env = dict(os.environ)
    # Do not let evaluator/provider credentials from the parent process cross
    # into the Candidate.  The only model credential visible to OpenClaw is a
    # non-secret placeholder; the evaluator-owned broker keeps the real key.
    for key in list(env):
        upper = key.upper()
        if "UPSTREAM" in upper and any(marker in upper for marker in ("KEY", "TOKEN", "URL")):
            env.pop(key, None)
        elif upper in {"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY"}:
            env.pop(key, None)
    env.update({
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(config),
        "OPENAI_API_KEY": PLACEHOLDER,
        "OPENAI_BASE_URL": broker.removesuffix("/v1/responses"),
        "AGENTSWE_RESPONSES_BASE_URL": broker,
        "AGENTSWE_REQUIRED_MODEL": MODEL,
        "AGENTSWE_REQUIRED_REASONING_EFFORT": EFFORT,
        # The benchmark enters through the internal webchat Gateway RPC.  No
        # external account/channel connector is part of the case, so defer
        # their startup just as OpenClaw's own Gateway health probes do.  This
        # lets the core Gateway publish readiness before optional sidecars are
        # loaded and avoids treating sidecar warmup as Candidate behavior.
        "OPENCLAW_SKIP_CHANNELS": "1",
        "PATH": f"{runtime['root']}/bin:{env.get('PATH', '')}",
        "HOME": str(state / "home"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "TMPDIR": str(state / "tmp"),
    })
    for key in ("HOME", "XDG_CACHE_HOME", "TMPDIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def rpc_command(node: Path, product: Path, port: int, token: str, method: str, params: dict[str, object], timeout: int = 300000) -> list[str]:
    return [str(node), str(product / "openclaw.mjs"), "gateway", "call", method, "--url", f"ws://127.0.0.1:{port}", "--token", token, "--params", json.dumps(params, separators=(",", ":")), "--timeout", str(timeout), "--json"]


def health_command(node: Path, product: Path, port: int, token: str, timeout_ms: int = 15000) -> list[str]:
    return [str(node), str(product / "openclaw.mjs"), "gateway", "health", "--url", f"ws://127.0.0.1:{port}", "--token", token, "--timeout", str(timeout_ms), "--json"]


def run_rpc(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float,
            *, pass_fds: tuple[int, ...] = ()) -> dict[str, object]:
    started = time.monotonic()
    try:
        completed = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True,
                                   check=False, timeout=timeout, pass_fds=pass_fds)
        stdout, stderr = completed.stdout, completed.stderr
        try:
            payload: object = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        method = cmd[cmd.index("call") + 1] if "call" in cmd else "health"
        return {"method": method, "status": "ok" if completed.returncode == 0 else "error", "exit_code": completed.returncode, "duration_ms": round((time.monotonic() - started) * 1000), "response": payload if isinstance(payload, (dict, list)) else None, "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-2000:]}
    except subprocess.TimeoutExpired as exc:
        method = cmd[cmd.index("call") + 1] if "call" in cmd else "health"
        return {"method": method, "status": "timeout", "exit_code": 124, "duration_ms": round((time.monotonic() - started) * 1000), "response": None, "stdout_tail": str(exc.stdout or "")[-2000:], "stderr_tail": str(exc.stderr or "")[-2000:]}


def deadline_rpc(node: Path, product: Path, port: int, token: str, method: str,
                 params: dict[str, object], env: dict[str, str], deadline: float,
                 limit_seconds: float | None = None, sandbox=None) -> dict[str, object]:
    """One absolute remaining budget, consistently in CLI ms and Python s."""
    remaining = max(0.0, deadline - time.monotonic())
    seconds = min(remaining, limit_seconds) if limit_seconds is not None else remaining
    operation_deadline = min(deadline, time.monotonic() + seconds)
    # Do not submit a new agent operation after its execution budget is gone.
    if seconds < (1.0 if method == 'agent' else 0.001):
        return {'method': method, 'status': 'timeout', 'exit_code': 124, 'duration_ms': 0,
                'response': None, 'stdout_tail': '', 'stderr_tail': 'deadline exhausted before dispatch',
                'dispatched': False, 'timeout_seconds': 0}
    milliseconds = max(1, int(seconds * 1000))
    supplied = dict(params)
    if method == 'agent.wait':
        supplied['timeoutMs'] = min(int(supplied.get('timeoutMs', milliseconds)), milliseconds)
    elif method == 'agent':
        # The agent clock, not the whole case envelope: the lower broker refuses an
        # absolute deadline further than AGENT_CLOCK_SECONDS away (LOWER_MAX_SECONDS).
        supplied['timeout'] = min(int(supplied.get('timeout', AGENT_CLOCK_SECONDS)), max(1, int(seconds)))
    command = (health_command(node, product, port, token, milliseconds) if method == 'health' else
               rpc_command(node, product, port, token, method, supplied, milliseconds))
    if sandbox is None:
        # Kept for pure command/deadline unit tests; the real case launcher
        # always supplies its owned ProductSandbox, without a host fallback.
        result = run_rpc(command, product, env, seconds)
    else:
        with sandbox.rpc_command(command) as (wrapped, descriptors):
            seconds = max(0, operation_deadline - time.monotonic())
            if seconds <= 0:
                raise TimeoutError('case budget exhausted before sandbox RPC dispatch')
            result = run_rpc(wrapped, product, {'PATH': '/usr/bin:/bin'}, seconds,
                             pass_fds=descriptors)
    result.update(dispatched=True, timeout_seconds=seconds, cli_timeout_ms=milliseconds)
    return result


def wait_gateway_health(gateway, log_path: Path, node: Path, product: Path,
                        port: int, token: str, env: dict[str, str], case_deadline: float,
                        sandbox=None, startup_deadline: float | None = None):
    """The ready marker and all health attempts share the public 120s budget."""
    deadline = min(case_deadline, time.monotonic() + GATEWAY_STARTUP_SECONDS)
    if startup_deadline is not None:
        deadline = min(deadline, startup_deadline)
    ready = False
    while time.monotonic() < deadline and gateway.poll() is None:
        try:
            content = log_path.read_text(encoding='utf-8', errors='ignore')
            if '[gateway] ready' in content or ' gateway ready' in content:
                ready = True
                break
        except OSError:
            pass
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    health = None
    if ready:
        for _ in range(5):
            if gateway.poll() is not None or time.monotonic() >= deadline:
                break
            health = deadline_rpc(node, product, port, token, 'health', {}, env, deadline,
                                  HEALTH_RPC_SECONDS, sandbox=sandbox)
            if health.get('status') == 'ok':
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    return health


def materialize_product(source: Path, output: Path, runtime: dict[str, object]) -> Path:
    """Copy the complete prepared image; never substitute baseline binaries."""
    source = source.resolve()
    product = output / "runtime_product"
    if product.exists():
        raise FileExistsError('case runtime product already exists; preserve prior evidence')
    if not (source/'dist/entry.js').is_file() or not (source/'node_modules').is_dir():
        raise RuntimeError('prepared Candidate runtime is incomplete; baseline fallback is forbidden')
    for path in source.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(source):
            raise ValueError('prepared runtime link escapes product: '+path.relative_to(source).as_posix())
    expected = tree_digest(source)
    shutil.copytree(source, product, symlinks=True)
    make_materialized_tree_writable(product)
    if tree_digest(product) != expected or tree_digest(source) != expected:
        raise RuntimeError('prepared Candidate runtime changed during materialization')
    return product


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--gateway-token")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--params", type=Path)
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--private-values", nargs="*", default=[])
    parser.add_argument("--private-values-file", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--case-deadline-monotonic", type=float)
    parser.add_argument("--launcher-deadline-monotonic", type=float)
    args = parser.parse_args()

    # One identity scope per evaluation, bound before any relay is created.
    # Independent Candidates run the same case from the same pristine repository
    # and issue byte-identical first requests, so without this they share one
    # logical-request identity and one transient upstream failure refuses every
    # later Candidate for the rest of the run.
    sandbox_transport.set_evaluation_scope(hashlib.sha256(
        ("agentswe-lower-evaluation/v1\x00" + str(args.case) + "\x00" +
         str(Path(args.output).resolve())).encode()).hexdigest())

    started = time.monotonic()
    if args.case_deadline_monotonic is not None and not math.isfinite(args.case_deadline_monotonic):
        raise SystemExit('case deadline must be finite')
    case_deadline = min(started + CASE_TIMEOUT_SECONDS, args.case_deadline_monotonic) if args.case_deadline_monotonic is not None else started + CASE_TIMEOUT_SECONDS
    if not math.isfinite(case_deadline) or case_deadline <= started:
        raise SystemExit('case deadline is invalid or already exhausted')
    # Reserve stopped-product hashing/collection as well as Gateway teardown.
    # The outer runner retains the original case deadline; no stage resets it.
    launcher_deadline=case_deadline-FINALIZATION_SECONDS
    if args.launcher_deadline_monotonic is not None:
        supplied=args.launcher_deadline_monotonic
        if not math.isfinite(supplied) or supplied>launcher_deadline:
            raise SystemExit('launcher deadline cannot borrow case finalization reserve')
        launcher_deadline=supplied
    if launcher_deadline-GATEWAY_CLEANUP_SECONDS<=started:
        raise SystemExit('case preparation left no product execution budget')
    work_deadline = launcher_deadline - GATEWAY_CLEANUP_SECONDS

    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    artifact_dir = (args.artifact_dir or output / 'workspace').resolve(); artifact_dir.mkdir(parents=True, exist_ok=True)
    source_product = args.product.resolve(); case = args.case.resolve()
    broker_before: dict[str, object] | None = None
    broker_stats_error: str | None = None
    try:
        broker_before = read_broker_stats(args.broker_endpoint,
            timeout=min(5, work_deadline-time.monotonic()))
        write_manifest(output / "broker_before.json", broker_before)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        broker_stats_error = f"{type(exc).__name__}: {exc}"
        write_manifest(output / "broker_before.json", {"status": "unavailable", "error": broker_stats_error})
    runtime = resolve(args.runtime, source_product)
    write_manifest(output / "runtime_manifest.json", runtime)
    product = materialize_product(source_product, output, runtime)
    state = (args.state_dir or output / "gateway_state").resolve()
    token = args.gateway_token or secrets.token_urlsafe(24)
    port = args.port or free_port()
    task_path = (args.task_file or (case / "input.md")).resolve()
    task = task_path.read_text(encoding="utf-8")
    # The output directory is intentionally explicit in the model-visible
    # request.  The evaluator still treats the artifact as absent unless the
    # real OpenClaw agent writes it there.
    task += (
        "\n\nRuntime delivery contract (incremental, and not conditional on finishing): "
        "as soon as you have the first response from the product, write "
        f"agent_result.json and run_report.json in {artifact_dir} with "
        'decision_state "partial" and whatever you have established so far, and refresh '
        "both files after every further step. Do not hold them back until the case is "
        "complete: the case has a wall clock, an absent or stale agent_result.json is "
        "recorded as no delivery at all, and an honest partial result is scored on what "
        "it evidences. Set decision_state to \"complete\" only on your final refresh, and "
        "list everything untried or unverified in uncertainty. Do not place "
        "evaluator-private facts, secrets, tokens, or raw control payloads in either file."
    )
    params = json.loads(args.params.read_text(encoding="utf-8")) if args.params else {
        "message": task,
        "agentId": "main",
        "sessionKey": f"agent:main:{case.name}",
        "channel": "webchat",
        "accountId": "default",
        "to": "case-peer",
        "timeout": 180,
        "idempotencyKey": f"{case.name}-{secrets.token_hex(8)}",
    }
    # Public Gateway callers cannot supply provider/model overrides. The
    # evaluator pins the model in the isolated config and the broker pins the
    # upstream request, so strip accidental overrides from supplied fixtures.
    params.pop("model", None); params.pop("provider", None)
    bridge_port = free_port()
    while bridge_port == port:
        bridge_port = free_port()
    isolated_endpoint = f'http://127.0.0.1:{bridge_port}/v1/responses'
    config = write_config(state, artifact_dir, isolated_endpoint, token)
    env = env_for(state, config, isolated_endpoint, runtime)
    node = Path(str(runtime["node"])); log_path = output / "gateway.log"
    gateway_cmd = [str(node), str(product / "openclaw.mjs"), "gateway", "--port", str(port), "--bind", "loopback", "--auth", "token", "--token", token, "--allow-unconfigured"]
    trajectory: list[dict[str, object]] = []
    gateway = None
    embedded = None
    sandbox = None
    native_case = None
    report: dict[str, object] = {"schema_version": "openclaw-run-report-v2", "case_id": case.name, "model": MODEL, "reasoning_effort": EFFORT, "state_dir": str(state), "config_path": str(config), "port": port, "command_entry": PRODUCTION_ENTRY, "broker_endpoint_is_evaluator_owned": True, "broker_stats_before": str(output / "broker_before.json"), "errors": []}
    before_runtime = broker_before.get('runtime', {}) if isinstance(broker_before, dict) else {}
    report['broker_in_flight_before'] = before_runtime.get('in_flight_calls') if isinstance(before_runtime, dict) else None
    if broker_stats_error:
        report["errors"].append(f"broker stats before unavailable: {broker_stats_error}")
    report['timing_contract'] = {'case_limit_seconds': CASE_TIMEOUT_SECONDS,
        'agent_clock_seconds': AGENT_CLOCK_SECONDS,
        'setup_allowance_seconds': SETUP_ALLOWANCE_SECONDS,
        'agent_clock_anchor': 'lower agent sandbox creation, after evaluator setup',
        'gateway_startup_limit_seconds': GATEWAY_STARTUP_SECONDS,
        'health_rpc_limit_seconds': HEALTH_RPC_SECONDS,
        'gateway_cleanup_reserved_seconds': GATEWAY_CLEANUP_SECONDS,
        'post_launcher_finalization_reserved_seconds': FINALIZATION_SECONDS,
        'absolute_case_deadline_monotonic': case_deadline,
        'absolute_launcher_deadline_monotonic': launcher_deadline,
        'absolute_product_deadline_monotonic': work_deadline,
        'absolute_agent_deadline_monotonic': None,
        'evaluator_setup_seconds': None,
        'setup_allowance_exceeded': None,
        'scope': 'evaluator setup has its own allowance and is not charged to the agent clock; '
                 'the agent clock runs from its sandbox creation to absolute_agent_deadline_monotonic; '
                 'RPC/health/cleanup use remaining time; outer resource enforcement still requires '
                 'independent acceptance'}
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def on_sigterm(_signum, _frame):
        # The outer launcher owns this process. Do not leave the separately
        # session-owned Gateway running when it terminates us at its deadline.
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        if broker_before is None or report['broker_in_flight_before']:
            raise RuntimeError('lower broker starting statistics are unavailable or contain unresolved requests')
        if time.monotonic() >= work_deadline:
            raise TimeoutError('case budget exhausted during launcher preparation')
        from evaluator.native_case_runtime import NativeCaseRuntime
        if args.private_values_file:
            facts_payload=json.loads(args.private_values_file.read_text(encoding='utf-8'))
            if not isinstance(facts_payload,dict) or facts_payload.get('bundle_version')!='openclaw-native-case-v2':
                raise ValueError('stale private case bundle cannot be silently reinterpreted')
            facts=PrivateFacts(**facts_payload)
            if facts.case_id!=case.name:
                raise ValueError('private case identity differs from requested case')
        else:
            facts=make_private_facts(case.name)
        native_case=NativeCaseRuntime(facts=facts,product=product,state=state,workspace=artifact_dir,
            runtime=runtime,output=output,endpoint=args.broker_endpoint,deadline=work_deadline,
            gateway_token=token,primary_port=port,product_readonly=True)
        native_case.start()
        cluster=native_case.cluster
        sandbox=cluster.sandbox; gateway=cluster.primary; env=cluster.env
        port=cluster.ports['B']; token=cluster.token
        log_path=output/'gateway-b.log'
        health=cluster.health.get('B')
        report['native_cluster_health']=cluster.health
        params['message']=task+(
            '\n\nTwo real Gateways and a task context service are available. Read case_view.json, then use '
            '/usr/bin/python3 -I -B /agentswe/native_client.py context to obtain current task handles, '
            'source assets and external events. The first context read creates the initial task once; '
            'later context reads never refresh its lease. Setup remains inside the same case deadline. '
            'For rpc, provide one JSON object with gateway, method '
            'and exact native params; for batch provide one or two such objects to run concurrently. '
            'Use {$handle: name} only for evaluator-issued opaque values. The client chooses no '
            'revision, epoch, idempotency key, operation or recovery sequence for you. '
            'Deliver the contents of requested-result.txt on the original source route and attach '
            'case-attachment.bin when the context supplies it. Re-read context for new authorization '
            'and callback events. Keep all authority material, bodies and internal routes out of reports. '
            'If the edited product lacks the required native feature, report the limitation and partial '
            'outcome honestly; do not replace the product or fabricate its state. '
            'agent_result.json must use schema_version="openclaw-agent-result-v1", this case_id, '
            'decision as a string, decision_state as "partial" on every refresh before your '
            'last one and "complete" only on that last one, observed_actions as an array of '
            'strings, evidence as objects '
            'with kind and reference strings (use actual rpc-N references), and uncertainty as an '
            'array of strings. run_report.json needs status, commands and errors. '
            'Both files must be authored by your OpenClaw tool actions in the requested workspace.')
        params['sessionKey']=native_case.session_key
        params['channel']='webchat'; params['accountId']=facts.route_account
        params['to']=native_case.channel['peer_id']
        params['deliver']=False
        if facts.route_thread: params['threadId']=facts.route_thread
        else: params.pop('threadId',None)
        from lower_agent.embedded_agent import EmbeddedAgent
        report['command_entry']=PRODUCTION_ENTRY
        report["gateway_pid"] = gateway.pid
        report["health"] = health
        if health is None or health.get("status") != "ok":
            report["status"] = "blocked"
            report["errors"].append("Gateway did not publish and pass production health readiness")
        else:
            embedded=EmbeddedAgent(cluster,message=params['message'],session_key=native_case.session_key)
            # The agent clock is anchored inside EmbeddedAgent; publish what the
            # evaluator's own setup actually cost so the split is auditable.
            setup_seconds=embedded.clock_started-started
            report['timing_contract'].update(
                absolute_agent_deadline_monotonic=embedded.deadline,
                evaluator_setup_seconds=setup_seconds,
                setup_allowance_exceeded=setup_seconds>SETUP_ALLOWANCE_SECONDS)
            if setup_seconds>SETUP_ALLOWANCE_SECONDS:
                report['errors'].append(
                    'evaluator setup exceeded its %ss allowance (%.1fs); the agent clock was '
                    'shortened by the case envelope' % (SETUP_ALLOWANCE_SECONDS, setup_seconds))
            embedded.start()
            native_run=embedded.wait()
            report['native_agent']=native_run
            report['status']='completed' if native_run['status']=='completed' else 'partial'
            if native_run['status']!='completed':
                report['errors'].append('native embedded Agent '+native_run['status'])
    except Exception as exc:
        report["status"] = "blocked"; report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if embedded is not None:
            try:
                report['native_agent']=embedded.close(min(GATEWAY_CLEANUP_SECONDS,
                    max(0,launcher_deadline-time.monotonic())))
                report['sandbox']=embedded.sandbox.attestation
            except Exception as exc:
                report['status']='blocked'
                report['errors'].append('embedded Agent cleanup unverified: '+type(exc).__name__)
        if native_case is not None:
            try:
                report['native_case']=native_case.finish()
                sandbox=native_case.cluster.sandbox
                if embedded is None:
                    report['sandbox']=sandbox.attestation
                if (report['native_case'].get('infrastructure_errors')
                    or report['native_case']['world'].get('failure')
                    or not report['native_case']['world'].get('cleanup_complete')):
                    report['status']='blocked'
                    report['errors'].append('native fixture or lifecycle cleanup is unverified')
            except Exception as exc:
                report['status']='blocked'
                report['errors'].append(f'native case cleanup unverified: {type(exc).__name__}')
        elif sandbox is not None:
            try:
                report['sandbox'] = sandbox.close(min(GATEWAY_CLEANUP_SECONDS, max(0, launcher_deadline - time.monotonic())))
            except Exception as exc:
                report['status'] = 'blocked'
                report['errors'].append(f'sandbox cleanup unverified: {type(exc).__name__}')
        report["gateway_exit_code"] = gateway.returncode if gateway else None
        signal.signal(signal.SIGTERM, previous_sigterm)
        report['timing_contract']['launcher_elapsed_seconds'] = time.monotonic() - started
    report["trajectory"] = [{k: v for k, v in item.items() if k not in {"stdout_tail", "stderr_tail"}} for item in trajectory]
    report["gateway_log"] = str(log_path)
    broker_after_value: dict[str, object] | None = None
    try:
        broker_after = read_broker_stats(args.broker_endpoint,
            timeout=min(5, launcher_deadline-time.monotonic()))
        broker_after_value = broker_after
        write_manifest(output / "broker_after.json", broker_after)
        report["broker_stats_after"] = str(output / "broker_after.json")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        after_error = f"{type(exc).__name__}: {exc}"
        write_manifest(output / "broker_after.json", {"status": "unavailable", "error": after_error})
        report["broker_stats_after"] = str(output / "broker_after.json")
        report["errors"].append(f"broker stats after unavailable: {after_error}")
    delta = broker_stats_delta(broker_before, broker_after_value)
    report["broker_stats_delta"] = delta
    after_runtime = broker_after_value.get('runtime', {}) if isinstance(broker_after_value, dict) else {}
    report['broker_in_flight_after'] = after_runtime.get('in_flight_calls') if isinstance(after_runtime, dict) else None
    report['broker_usage_unknown_calls_after'] = after_runtime.get('usage_unknown_calls') if isinstance(after_runtime, dict) else None
    private_values = list(args.private_values)
    private_check_complete = True
    if args.private_values_file:
        try:
            private_payload = json.loads(args.private_values_file.read_text(encoding="utf-8"))
            if isinstance(private_payload, dict):
                # Account labels and attachment hashes are legitimate public
                # metadata, not raw authority secrets. Do not reject a valid
                # report merely for containing "default" or its media hash.
                # oracle_decision is the fixed label "evaluator-only", not a
                # secret answer. Actual authority secrets come from the native world.
                private_keys = {"route_peer", "route_thread", "task_nonce", "callback_token"}
                private_values.extend(str(private_payload[key]) for key in private_keys if isinstance(private_payload.get(key), str))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            private_check_complete = False
            report["errors"].append(f"private-value validation unavailable: {type(exc).__name__}")
    if native_case is not None:
        private_values.extend(native_case.private_values())
    authored_result = artifact_dir / "agent_result.json"
    authored_report = artifact_dir / "run_report.json"
    from evaluator.candidate_outcome import inspect_core
    admission=inspect_core(authored_result,case.name,deadline=launcher_deadline,private_values=private_values)
    admission['private_value_check_complete']=private_check_complete
    if admission['state']=='semantic_review':
        result_valid, result_error = validate_agent_result(authored_result, case.name, private_values)
    else:
        result_valid, result_error = False, admission['reason']
    auxiliary=inspect_core(authored_report,case.name,deadline=launcher_deadline,private_values=private_values)
    if auxiliary['state']=='semantic_review':
        run_report_valid, run_report_error = validate_agent_run_report(authored_report, private_values)
    else:
        run_report_valid, run_report_error = False, auxiliary['reason']
    artifact_valid = result_valid and run_report_valid
    artifact_error = result_error if not result_valid else run_report_error if not run_report_valid else None
    report["artifact_contract"] = {"artifact_dir": str(artifact_dir), "agent_result": {"path": str(authored_result), "present": authored_result.is_file(), "valid": result_valid, "error": result_error}, "run_report": {"path": str(authored_report), "present": authored_report.is_file(), "valid": run_report_valid, "error": run_report_error}, "valid": artifact_valid, "error": artifact_error}
    report['artifact_contract']['core_admission']=admission
    report['artifact_contract']['auxiliary_observation']=auxiliary
    classification, classification_reason = classify_execution(report, delta, admission['state']=='semantic_review', artifact_error)
    if admission['state']=='evidence_unavailable' or not private_check_complete:
        classification, classification_reason = 'launcher_infrastructure_error', 'core artifact or privacy observation is unavailable'
    sandbox_evidence = report.get('sandbox', {})
    if not sandbox_evidence.get('valid') or not sandbox_evidence.get('gateway_wrapper_reaped'):
        classification, classification_reason = 'launcher_infrastructure_error', 'product sandbox startup or cleanup is unverified'
    elif sandbox_evidence.get('transport', {}).get('incomplete'):
        classification, classification_reason = 'broker_infrastructure_error', 'fixed lower transport recorded an incomplete or unresolved request'
    gateway_sandboxes=report.get('native_case',{}).get('cluster',{}).get('sandbox_evidence',[])
    if not gateway_sandboxes or any(not item.get('valid') or not item.get('gateway_wrapper_reaped')
                                   for item in gateway_sandboxes):
        classification, classification_reason = 'launcher_infrastructure_error', 'Gateway sandbox startup or cleanup is unverified'
    elif any(item.get('transport',{}).get('incomplete') for item in gateway_sandboxes):
        classification, classification_reason = 'broker_infrastructure_error', 'a Gateway model transport has an unresolved request'
    if broker_before is None or broker_after_value is None:
        classification, classification_reason = 'broker_infrastructure_error', 'before/after lower broker statistics are unavailable'
    elif any(isinstance(report.get(field), int) and report[field] > 0
             for field in ('broker_in_flight_before', 'broker_in_flight_after')):
        classification, classification_reason = 'broker_infrastructure_error', 'lower broker has an unresolved request at a case boundary'
    report["classification"] = classification
    report["classification_reason"] = classification_reason
    report["formal_result_claimed"] = False
    write_manifest(output / "run_report.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if classification == "candidate_behavior_observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
