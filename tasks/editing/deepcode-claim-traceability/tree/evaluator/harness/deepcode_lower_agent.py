#!/usr/bin/env python3
"""Launch the patched DeepCode lower agent, never an external replacement."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

MODEL = "deepseek-flash"
EFFORT = "high"
# The evaluator forces reasoning effort "high" (EFFORT, and again on the wire in
# broker_server.py).  DeepCode's stock AgentDefaults.max_tokens is 8192
# (input/repository/core/config.py:83), the out-of-the-box default for a
# NON-reasoning model, and it becomes max_output_tokens on the Responses body
# (core/providers/openai_compat.py:585).  On deepseek-flash at effort "high" the
# reasoning tokens alone reached 8192 once the agent's context passed ~70 k input
# tokens, so the provider returned status=incomplete /
# incomplete_details.reason=max_output_tokens with an `output` holding a single
# `reasoning` item and no `message`: a 100 %-reasoning, zero-content answer.  17 of
# the 18 hidden case runs of 0920-fh-001/002/003 died that way.  The forced effort
# is ours, so the budget that pays for it has to be ours too.  32000 matches the
# ceiling already pinned for the Result judge; the peak observed NON-truncated
# reasoning in those runs was ~6.4 k, so this leaves ~5x headroom.
# maxTokens is the camelCase alias of AgentDefaults.max_tokens
# (core/config.py:65 alias_generator=to_camel, :83), and resolve_phase() feeds it
# to GenerationSettings.max_tokens (core/config.py:799) unchanged: the connection-
# profile clamp at core/providers/profiles.py:197 is not on this path, because the
# evaluator config uses agents.defaults.provider + providers.custom, not a
# connection id.  Verified by resolving the written config with the product's own
# loader (checks_deepcode.py::check_config_alias).
MAX_OUTPUT_TOKENS = 32000
STATS_TOKEN = "stats-only-placeholder"
PLACEHOLDER_CREDENTIAL = "broker-only-placeholder"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _credential_environment_name(name: str) -> bool:
    upper = name.upper()
    return (
        any(marker in upper for marker in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD"))
        or upper.endswith("_KEY")
        or upper.startswith((
            "AWS_", "AZURE_", "GOOGLE_", "GITHUB_", "GH_", "HF_",
            "HUGGINGFACE_", "ANTHROPIC_", "OPENAI_", "GATEWAY_", "SERPER_",
            "COHERE_", "MISTRAL_", "GROQ_",
        ))
    )


def candidate_environment(source: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Return a credential-scrubbed environment for untrusted Candidate code."""
    removed = sorted(name for name in source if _credential_environment_name(name))
    clean = {name: value for name, value in source.items() if name not in removed}
    clean["OPENAI_API_KEY"] = PLACEHOLDER_CREDENTIAL
    return clean, removed


def broker_stats_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid broker endpoint: {endpoint!r}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/stats", "", ""))


def read_broker_stats(endpoint: str) -> dict[str, Any]:
    request = urllib.request.Request(
        broker_stats_url(endpoint),
        headers={"Authorization": f"Bearer {STATS_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats response must be an object")
    return value


def write_config(home: Path, endpoint: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "deepcode_config.json").write_text(json.dumps({
        "agents": {"defaults": {"provider": "custom", "model": MODEL, "reasoningEffort": EFFORT,
                                "maxTokens": MAX_OUTPUT_TOKENS}},
        "providers": {"custom": {"apiKey": PLACEHOLDER_CREDENTIAL, "apiBase": endpoint.removesuffix("/responses")}},
        "security": {"accessPreset": "full_access"}
    }, indent=2) + "\n", encoding="utf-8")


def build_command(repo: Path, task: str, workspace: Path, home: Path, endpoint: str, python_executable: Path) -> list[str]:
    write_config(home, endpoint)
    return [str(python_executable), str(repo / "deepcode.py"), "exec", task, "--workspace", str(workspace),
            "--json", "--trust", "--access", "full-access", "--model", MODEL, "--effort", EFFORT]


def _python_prefixes(python_executable: Path) -> list[Path]:
    probe = subprocess.run(
        [str(python_executable), "-I", "-S", "-c", "import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode:
        raise RuntimeError(f"could not inspect DeepCode Python runtime: {probe.stderr[-800:]}")
    values = json.loads(probe.stdout)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError("DeepCode Python runtime returned invalid prefix metadata")
    unique: list[Path] = [python_executable.parent.parent] if (python_executable.parent.parent / "pyvenv.cfg").is_file() else []
    for value in values:
        path = Path(value).resolve()
        if path not in unique and not str(path).startswith(("/usr", "/bin", "/lib")):
            unique.append(path)
    return unique


def sandbox_command(
    *,
    command: list[str],
    repository: Path,
    workspace: Path,
    home: Path,
    isolated: Path,
    python_executable: Path,
    relay=None,
) -> tuple[list[str], dict[str, object]]:
    """Wrap the Candidate process in an evaluator-owned mount sandbox.

    A new network namespace can reach only a fixed lower broker through UDS.  No evaluator source, canonical test root,
    prior run, or credential file is mounted.
    """
    executable = shutil.which("bwrap")
    if not executable:
        raise RuntimeError("bubblewrap is required for a real lower-agent run")
    fixed_command = list(command)
    replacements = {
        str(repository): "/candidate",
        str(workspace): "/workspace",
        str(home): "/deepcode-home",
    }
    for index, value in enumerate(fixed_command):
        for source, target in replacements.items():
            if value == source:
                fixed_command[index] = target
            elif value.startswith(source + os.sep):
                fixed_command[index] = target + value[len(source):]
    argv = [
        executable,
        "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc/hosts", "/etc/hosts",
        "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    runtime_mounts: list[str] = []
    for prefix in _python_prefixes(python_executable):
        argv.extend(["--ro-bind", str(prefix), str(prefix)])
        runtime_mounts.append(str(prefix))
    if relay is not None:
        argv += ['--dir','/run/agentswe','--ro-bind',str(relay.socket_path),'/run/agentswe/lower.sock',
            '--ro-bind',str(Path(__file__).with_name('transport_sandbox.py')),'/run/agentswe/transport.py']
        fixed_command = ['/usr/bin/python3','/run/agentswe/transport.py','--inside','--uds','/run/agentswe/lower.sock',
            '--preflight','/runtime/transport-preflight.json','--', *fixed_command]
    argv.extend([
        "--bind", str(repository), "/candidate",
        "--bind", str(workspace), "/workspace",
        "--bind", str(home), "/deepcode-home",
        "--bind", str(isolated), "/runtime",
        "--chdir", "/candidate",
        "--",
        *fixed_command,
    ])
    return argv, {
        "mechanism": "bubblewrap",
        "network": "isolated-fixed-lower-UDS-only",
        "candidate_mount": "/candidate",
        "workspace_mount": "/workspace",
        "deepcode_home_mount": "/deepcode-home",
        "runtime_mount": "/runtime",
        "python_runtime_read_only_mounts": runtime_mounts,
        "evaluator_source_mounted": False,
        "canonical_test_cases_mounted": False,
        "credential_file_mounted": False,
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


def broker_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    before_runtime = before.get("runtime", {}) if isinstance(before, dict) else {}
    after_runtime = after.get("runtime", {}) if isinstance(after, dict) else {}
    calls = int(after_runtime.get("calls", 0) or 0) - int(before_runtime.get("calls", 0) or 0)
    failures = int(after_runtime.get("failures", 0) or 0) - int(before_runtime.get("failures", 0) or 0)
    successful = int(after_runtime.get("successful_calls", 0) or 0) - int(before_runtime.get("successful_calls", 0) or 0)
    result = {"calls": calls, "failures": failures, "successful_calls": successful}
    for name in ("provider_failures", "credential_failures", "protocol_failures", "broker_failures",
                 "output_budget_truncations"):
        result[name] = int(after_runtime.get(name, 0) or 0) - int(before_runtime.get(name, 0) or 0)
    # D13: recovered upstream transport failures inside this case, counted from the rows.
    result["recovered_transport_calls"] = _d13_recovered_transport_calls(before, after)
    return result


LAUNCHER_RESERVE_SECONDS = 45  # product stops this long before the case deadline


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project-source", type=Path)
    parser.add_argument("--deepcode-home", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--python", dest="python_executable", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--sandbox", choices=("required", "off"), default="required")
    parser.add_argument("--case-deadline-monotonic", type=float)
    args = parser.parse_args(argv)
    repo, workspace, home, output = (p.resolve() for p in (args.repository, args.workspace, args.deepcode_home, args.output))
    # Do not call Path.resolve() here: venv/bin/python is commonly a symlink to
    # the host interpreter, and resolving it drops the venv's site-packages.
    python_executable = Path(args.python_executable or os.environ.get("DEEPCODE_PYTHON", sys.executable)).absolute()
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise SystemExit(f"DeepCode runtime Python is not executable: {python_executable}")
    workspace.mkdir(parents=True, exist_ok=True); output.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started_monotonic = time.monotonic()
    try:
        broker_before = read_broker_stats(args.broker_endpoint)
        write_json(output / "broker_before.json", broker_before)
        broker_stats_error = None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        broker_stats_error = f"{type(exc).__name__}: {exc}"
        broker_before = {"status": "unavailable", "error": broker_stats_error}
        write_json(output / "broker_before.json", broker_before)
    if args.project_source:
        source = args.project_source.resolve()
        if not source.is_dir(): raise SystemExit("project source is not a directory")
        shutil.copytree(source, workspace, dirs_exist_ok=True, symlinks=True)
    task = args.task_file.read_text(encoding="utf-8")
    isolated = output / "runtime"
    for name in ("home", "xdg_cache_home", "tmpdir", "xdg_config_home"):
        (isolated / name).mkdir(parents=True, exist_ok=True)
    env, removed_credential_names = candidate_environment(dict(os.environ))
    env.update({
        "HOME": "/runtime/home" if args.sandbox == "required" else str(isolated / "home"),
        "XDG_CACHE_HOME": "/runtime/xdg_cache_home" if args.sandbox == "required" else str(isolated / "xdg_cache_home"),
        "TMPDIR": "/runtime/tmpdir" if args.sandbox == "required" else str(isolated / "tmpdir"),
        "XDG_CONFIG_HOME": "/runtime/xdg_config_home" if args.sandbox == "required" else str(isolated / "xdg_config_home"),
        "DEEPCODE_HOME": "/deepcode-home" if args.sandbox == "required" else str(home),
        "OPENAI_API_KEY": PLACEHOLDER_CREDENTIAL,
        "OPENAI_BASE_URL": args.broker_endpoint.removesuffix("/responses"),
        "PYTHONPATH": "/candidate" if args.sandbox == "required" else str(repo),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(python_executable.parent) + os.pathsep + env.get("PATH", ""),
    })
    deadline = args.case_deadline_monotonic or time.monotonic() + min(args.timeout, 590)
    if args.sandbox != 'required':
        raise ValueError('Candidate execution requires namespace isolation')
    from transport_sandbox import FixedLowerRelay
    import hashlib
    from candidate_adapter import tree_digest
    context_identity=hashlib.sha256(json.dumps({'candidate_digest':tree_digest(repo), 'task_sha256':hashlib.sha256(task.encode()).hexdigest(),
        'case_id':args.case_id, 'fresh_case_output':str(output)},sort_keys=True).encode()).hexdigest()
    write_json(output / 'logical-context.json', {'identity':context_identity,'case_id':args.case_id,'candidate_digest_before':tree_digest(repo),
        'task_sha256':hashlib.sha256(task.encode()).hexdigest(),'new_lower_context':True})
    relay = FixedLowerRelay(args.broker_endpoint, context_identity=context_identity).start()
    command = build_command(repo, task, workspace, home, args.broker_endpoint, python_executable)
    sandbox: dict[str, object]
    if args.sandbox == "required":
        command, sandbox = sandbox_command(
            command=command,
            repository=repo,
            workspace=workspace,
            home=home,
            isolated=isolated,
            python_executable=python_executable, relay=relay,
        )
    else:
        sandbox = {
            "mechanism": "off",
            "evaluator_source_mounted": None,
            "canonical_test_cases_mounted": None,
            "credential_file_mounted": False,
        }
    timed_out = False
    spawn_error: str | None = None
    artifact_preexisting = (workspace / "agent_result.json").exists()
    probe_command, _ = sandbox_command(command=[str(python_executable), '-c',
        'import aiohttp,httpx,loguru,openai,pydantic_settings,yaml,rich'], repository=repo,
        workspace=workspace, home=home, isolated=isolated, python_executable=python_executable)
    preflight = subprocess.run(probe_command, env=env, text=True, capture_output=True,
        timeout=min(30, max(.001, deadline-time.monotonic())), check=False)
    preflight_evidence = {"valid": preflight.returncode == 0, "exit_code": preflight.returncode,
        "stderr_tail": preflight.stderr[-1500:], "kind": "evaluator_runtime_dependency_imports"}
    execution_attempted = False
    try:
        if preflight.returncode != 0:
            proc = subprocess.CompletedProcess(command, 125, "", preflight.stderr)
        else:
            execution_attempted = True
            # D14 launcher reserve (2026-09-20): the callers wait for THIS launcher only until
            # `deadline`, the same instant they hand down as the product's deadline, so a
            # product that ran to the deadline had the launcher killed under it before the
            # timed-out record could be written (readiness 0920-hd-003 round 2 attempt 1 ->
            # launcher_infrastructure_error, exit 124, retried as infrastructure). The product
            # now stops LAUNCHER_RESERVE_SECONDS earlier, in its own session so its whole
            # tree dies with it, and the launcher keeps the reserve for its bookkeeping.
            product_budget = max(.001, deadline - time.monotonic() - LAUNCHER_RESERVE_SECONDS)
            child = subprocess.Popen(command, cwd=repo, env=env, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
            try:
                out, err = child.communicate(timeout=product_budget)
                proc = subprocess.CompletedProcess(command, child.returncode, out or "", err or "")
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    child.kill()
                try:
                    out, err = child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                proc = subprocess.CompletedProcess(command, 124, out or "", (err or "") or "timeout")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        proc = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "timeout")
    except OSError as exc:
        spawn_error = f"{type(exc).__name__}: {exc}"
        proc = subprocess.CompletedProcess(command, 125, "", spawn_error)
    relay.close()
    (output / "transport-relay.json").write_text(json.dumps({"events":relay.events,"errors":relay.errors},indent=2)+"\n")
    (output / "stdout.jsonl").write_text(proc.stdout or "", encoding="utf-8"); (output / "stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    answer: dict[str, Any] = {}
    try: answer = json.loads((workspace / "agent_result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): pass
    if not isinstance(answer,dict):answer={}
    result = {"schema_version": "deepcode-agentloop-case-result-v1", "candidate_exit_code": proc.returncode,
              "case_id": args.case_id,
              "execution_attempted": execution_attempted, "environment_preflight": preflight_evidence,
              "artifact_preexisting_before_launch": artifact_preexisting,
              "contract_valid": answer.get("schema_version") == "deepcode-agentloop-result/v1" and answer.get("case_id") == args.case_id, "answer": answer,
              "model_protocol": {"model": MODEL, "reasoning_effort": EFFORT, "transport": "evaluator-owned-broker"},
              "runtime": {"python": str(python_executable), "isolated_root": str(isolated), "sandbox": sandbox,
                          "started_at": started_at, "finished_at": utc_now(),
                          "duration_seconds": round(time.monotonic() - started_monotonic, 6),
                          "timed_out": timed_out, "spawn_error": spawn_error},
              "credential_isolation": {"candidate_credential": "placeholder-only", "placeholder_environment": ["OPENAI_API_KEY"],
                                       "removed_environment_names": removed_credential_names, "real_credential_exposed": False},
              "artifact": str(workspace / "agent_result.json"), "stdout": str(output / "stdout.jsonl"), "stderr": str(output / "stderr.log"),
              "broker_before": str(output / "broker_before.json")}
    try:
        broker_after = read_broker_stats(args.broker_endpoint)
        write_json(output / "broker_after.json", broker_after)
        result["broker_after"] = str(output / "broker_after.json")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        after_error = f"{type(exc).__name__}: {exc}"
        broker_after = {"status": "unavailable", "error": after_error}
        write_json(output / "broker_after.json", broker_after)
        result["broker_after"] = str(output / "broker_after.json")
    if broker_stats_error:
        result["broker_stats_error"] = broker_stats_error
    try:
        delta = broker_delta(broker_before, broker_after)
        result["broker_delta"] = delta
        # Evaluator-owned, non-accusatory reason the Builder can act on: the lower
        # agent's turns were cut short by OUR forced-effort output budget, not by
        # the product.  Surfaced in the accepted-submission feedback by
        # public_feedback.public_record.
        result["lower_agent_output_truncated"] = int(delta.get("output_budget_truncations", 0) or 0) > 0
        if broker_stats_error or broker_after.get("status") == "unavailable":
            result["classification"] = "broker_infrastructure_error"
        elif delta["credential_failures"] > 0:
            result["classification"] = "credential_infrastructure_error"
        elif delta["protocol_failures"] > 0:
            result["classification"] = "protocol_infrastructure_error"
        elif delta["broker_failures"] > 0:
            result["classification"] = "broker_infrastructure_error"
        elif (timed_out and delta["successful_calls"] > 0
              and delta["provider_failures"] <= _d48_tolerated(delta)
              and delta["failures"] <= _d48_tolerated(delta)):
            # D48 (policy D14): the launcher kills the product at
            # `deadline - LAUNCHER_RESERVE_SECONDS` (:428).  A lower request still in
            # flight dies with its client and broker_server.py:464/470 books it
            # `failure_domain="provider"`, so the branch below used to outrank the
            # candidate_behavior_failure branch at the bottom and void a case that had
            # spent its whole budget.  Budget exhaustion is a Candidate outcome.
            result["classification"] = "candidate_behavior_failure"
            result["case_budget_exhausted"] = True
        elif (delta["provider_failures"] > delta.get("recovered_transport_calls", 0)
              or delta["failures"] > delta.get("recovered_transport_calls", 0)):
            result["classification"] = "provider_failure"
        elif preflight.returncode != 0:
            result["classification"] = "evaluator_infrastructure_error"
        elif spawn_error:
            result["classification"] = "launcher_infrastructure_error"
        elif timed_out or delta["successful_calls"] <= 0 or not result["contract_valid"] or proc.returncode != 0:
            result["classification"] = "candidate_behavior_failure"
        else:
            result["classification"] = "candidate_valid"
    except (OSError, ValueError, TypeError) as exc:
        result["classification"] = "evaluator_infrastructure_error"
        result["classification_error"] = f"{type(exc).__name__}: {exc}"
    result["infra_valid"] = result.get("classification", "").startswith("candidate_")
    write_json(output / "execution_observation.json", result)
    from execution_evidence import attest
    from candidate_adapter import tree_digest
    result = attest(result, output=output, workspace=workspace, case_id=args.case_id, candidate_digest=tree_digest(repo))
    write_json(output / "result.json", result)
    print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if proc.returncode == 0 else 1


# --- D48 (2026-09-21) evaluator-owned deadline kills --------------------------------
def _d48_tolerated(delta):
    """Failures this case may carry without being called a provider outage.

    D13 already tolerates a recovered upstream transport failure.  D48 adds the
    ONE request the evaluator's own product kill can catch in flight: the launcher
    kills a single product process, so at most one of its requests is cut.
    """
    return int(delta.get("recovered_transport_calls", 0) or 0) + 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--owned-case' in argv:
        argv.remove('--owned-case')
        return _main(argv)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=600)
    parser.add_argument('--case-deadline-monotonic',type=float)
    args,_=parser.parse_known_args(argv)
    from owned_resources import run_owned
    # run_owned reserves 10 s of its own; keep a further 30 s so the TimeoutExpired
    # branch below can record the timeout before the scope SIGTERMs this driver.
    deadline=args.case_deadline_monotonic or time.monotonic()+min(args.timeout,600)-40
    if args.case_deadline_monotonic is None: argv += ['--case-deadline-monotonic',str(deadline)]
    proc,att=run_owned([sys.executable,'-I',str(Path(__file__).resolve()),'--owned-case',*argv],
        cwd=Path('/'),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
        output=args.output.resolve().with_name(args.output.name+'-resources'),timeout=min(args.timeout,600))
    (args.output.resolve().parent/(args.output.name+'-controller.stdout.log')).write_text(proc.stdout or '')
    (args.output.resolve().parent/(args.output.name+'-controller.stderr.log')).write_text(proc.stderr or '')
    print(proc.stdout or '',end='')
    return proc.returncode


if __name__ == "__main__": raise SystemExit(main())
