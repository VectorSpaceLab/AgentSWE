#!/usr/bin/env python3
"""Build the real DeepTutor mastery-agent command for an isolated case.

The command is intentionally not executed by Stage A.  DeepTutor owns the
agent loop, tool selection, state mutation, and final result; this module only
provides the production entry seam used by the evaluator launcher.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
import subprocess
from typing import Any

try:
    from .isolated_runtime import sandbox_command
    from .lower_transport import UnixHTTPRelay
    from .protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_TOKEN, write_json
except ImportError:  # pragma: no cover
    from isolated_runtime import sandbox_command
    from lower_transport import UnixHTTPRelay
    from protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_TOKEN, write_json  # type: ignore

try:
    from .runtime_probe import probe_runtime
except ImportError:  # pragma: no cover
    from runtime_probe import probe_runtime  # type: ignore


def command(repository: Path, prompt: Path, output: Path, python_executable: str, *, session_id: str | None = None) -> list[str]:
    case_id = output.name
    message = prompt.read_text(encoding="utf-8").rstrip() + (
        "\n\n## Required terminal artifact protocol\n\n"
        "Complete the product task through the mastery tools, then make your "
        "final user-facing response exactly one JSON object (no Markdown fence) "
        "with these top-level fields: schema_version, case_id, status, summary, "
        "and artifacts. The case_id must be "
        f"{case_id!r}. Put the real tool-returned evidence and learner advice "
        "inside that object. Do not invent identifiers, expected outcomes, or "
        "oracle values. If the task cannot be completed safely, report that "
        "honestly in status/summary and preserve the evidence boundary.\n"
    )
    argv = [
        python_executable,
        "-m",
        "deeptutor_cli",
        "run",
        "mastery_path",
        message,
        "--format",
        "json",
    ]
    if session_id:
        argv.extend(["--session", session_id])
    return argv


def _sandbox_command(argv: list[str], repository: Path, runtime_root: Path, python: str, *, relay_socket: Path | None = None) -> tuple[list[str], dict[str, Any]]:
    """Expose only Candidate code, this runtime, and Python dependencies."""
    return sandbox_command(argv, repository, runtime_root, python, relay_socket=relay_socket)


def _chat_base_url(responses_endpoint: str) -> str:
    """Convert the locked Responses URL to the nominal OpenAI base URL.

    DeepTutor's production client is a Chat Completions client and normally
    appends ``/chat/completions`` to this value.  The task-local
    ``sitecustomize`` below intercepts that client and sends the translated
    request to the exact Responses endpoint, so this value is kept in the
    catalog for truthful provider resolution and diagnostics.
    """
    endpoint = responses_endpoint.rstrip("/")
    if endpoint.endswith("/v1/responses"):
        return endpoint[: -len("/responses")]
    return endpoint


def _write_model_catalog(runtime_home: Path, broker_endpoint: str) -> Path:
    """Write the isolated active LLM profile consumed by DeepTutor.

    The API key is deliberately the candidate placeholder.  The evaluator
    broker is the only process allowed to hold the upstream credential.
    """
    catalog_path = runtime_home / "data" / "user" / "settings" / "model_catalog.json"
    catalog = {
        "version": 1,
        "services": {
            "llm": {
                "active_profile_id": "agentswe-broker-profile",
                "active_model_id": "agentswe-broker-model",
                "profiles": [
                    {
                        "id": "agentswe-broker-profile",
                        "name": "AgentSWE evaluator broker",
                        "binding": "openai",
                        "api_version": "",
                        "base_url": _chat_base_url(broker_endpoint),
                        "api_key": PLACEHOLDER_TOKEN,
                        "extra_headers": {},
                        "models": [
                            {
                                "id": "agentswe-broker-model",
                                "name": LOWER_MODEL,
                                "model": LOWER_MODEL,
                                "reasoning_effort": LOWER_EFFORT,
                            }
                        ],
                    }
                ],
            },
            "embedding": {"active_profile_id": None, "active_model_id": None, "profiles": []},
            "search": {"active_profile_id": None, "profiles": []},
            "tts": {"active_profile_id": None, "active_model_id": None, "profiles": []},
            "stt": {"active_profile_id": None, "active_model_id": None, "profiles": []},
            "imagegen": {"active_profile_id": None, "active_model_id": None, "profiles": []},
            "videogen": {"active_profile_id": None, "active_model_id": None, "profiles": []},
        },
    }
    write_json(catalog_path, catalog)
    return catalog_path


PUBLIC_REQUIRED_TOOLS = (
    "mastery_status",
    "mastery_quiz",
    "mastery_grade",
    "mastery_assess",
    "mastery_build",
    "mastery_remediation_status",
    "mastery_remediation_reset",
    "mastery_remediation_claim",
    "mastery_remediation_ack",
    "mastery_review_plan",
    "mastery_review_reschedule",
    "mastery_session_handoff",
    "mastery_session_resume",
    "mastery_session_handoffs",
    "mastery_session_abandon",
    "mastery_policy_status",
    "mastery_policy_publish",
    "mastery_learning_events",
    "mastery_learning_checkpoint",
    "mastery_learner_snapshot",
    "mastery_learner_snapshot_checkpoint",
    "mastery_learner_snapshot_attest",
    "mastery_learner_snapshot_restore",
    "mastery_learner_snapshot_witness",
    "mastery_learner_snapshot_verify",
    "mastery_learner_snapshot_chain_audit",
)


def _audit_public_tool_surface(repository: Path, output: Path) -> dict[str, Any]:
    """Audit only the public dev-required product tool names.

    This is an evaluator-side source audit, not a synthetic product result.
    It intentionally does not inspect hidden case definitions or oracle data.
    """
    mastery_dir = repository / "deeptutor" / "capabilities" / "mastery"
    tool_file = mastery_dir / "tools.py"
    # The product registry may deliberately keep the concrete tool classes in
    # a sibling module (for example ``extended_tools.py``) and assemble the
    # exported name/type tuples from that module.  Auditing only tools.py
    # creates a false candidate gap even when the real imported registry is
    # complete.  This remains a source audit of product-owned files; it does
    # not execute product code or synthesize tool results.
    source_files = sorted(mastery_dir.glob("*.py")) if mastery_dir.is_dir() else []
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in source_files
        if path.is_file()
    )
    present = [name for name in PUBLIC_REQUIRED_TOOLS if name in source]
    missing = [name for name in PUBLIC_REQUIRED_TOOLS if name not in source]
    audit = {
        "schema_version": "agentswe-deeptutor-public-tool-surface/v1",
        "scope": "public-dev-metadata-only",
        "source_file": str(tool_file),
        "source_files": [str(path) for path in source_files],
        "required_tools": list(PUBLIC_REQUIRED_TOOLS),
        "present_tools": present,
        "missing_tools": missing,
        "capability_gap": bool(missing),
    }
    write_json(output / "public_tool_surface.json", audit)
    return audit


def run(
    repository: Path,
    prompt: Path,
    output: Path,
    *,
    broker_endpoint: str,
    execute: bool = False,
    python_executable: str | None = None,
    source_repository: Path | None = None,
) -> dict[str, Any]:
    """One aggregate, owned 600s/4GiB budget for either dev or hidden."""
    if not execute:
        return _run_case(repository, prompt, output, broker_endpoint=broker_endpoint,
                         execute=False, python_executable=python_executable)
    try:
        from .owned_resources import run_owned
    except ImportError:
        from owned_resources import run_owned
    runtime_python = python_executable or os.environ.get("DEEPTUTOR_PYTHON", sys.executable)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # The actual dependency import probe is run inside the same scope by the
    # trusted driver, so setup cannot add an extra budget before the 600s case.
    preflight = {'status': 'performed_inside_case_scope'}
    driver = Path(__file__).with_name("resource_case_driver.py")
    argv = [runtime_python, "-I", str(driver), "--repository", str(repository.resolve()),
            "--prompt", str(prompt.resolve()), "--output", str(output), "--broker-endpoint", broker_endpoint,
            "--python", runtime_python]
    if source_repository is not None:argv.extend(["--source-repository",str(source_repository.resolve())])
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "DEEPTUTOR_CASE_ID", "DEEPTUTOR_RUNTIME_NONCE") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Never grant import diagnostics a new per-stage case budget. This is
    # conservative to the actual outer 600 s scope (590 s work + 10 s cleanup).
    env["AGENTSWE_CASE_DEADLINE_MONOTONIC"] = str(time.monotonic() + 590)
    try:
        completed, resources = run_owned(argv, cwd="/", env=env, output=output / "case_resources")
    except Exception as exc:
        value = {"classification": "evaluator_infrastructure_error", "executed": False,
                 "runtime_probe": preflight, "error": "owned case resource failure: " + str(exc)}
        write_json(output / "launcher_result.json", value)
        return value
    record_path = output / "launcher_result.json"
    value = json.loads(record_path.read_text()) if record_path.is_file() else {"executed": False}
    value["case_resource_contract"] = resources
    if resources["timed_out"]:
        # Partial broker requests may still be evaluator failures; the execution
        # classifier requires healthy transport/evidence before Candidate zero.
        value.update(classification="candidate_timeout", timed_out=True,
            product_terminal_failure=True, error="600 second aggregate case deadline exceeded")
        try:
            try:from .run_hidden import read_broker_stats
            except ImportError:from run_hidden import read_broker_stats
            stats=read_broker_stats(broker_endpoint)
            logical=json.loads((output/'logical-context.json').read_text())
            rows=[row for row in stats.get('logical_requests',{}).values() if row.get('context_id')==logical['context_id']]
            # D13: a request the agent recovered from -- transport error, later request in
            # this same context completed -- is noise; a pending or unknown one is not.
            recovered=_d13_recovered_transport_calls(None,{'requests':[dict(row.get('result') or {},request_sha256=row.get('request_sha256')) for row in rows]})
            noncompleted=sum(row['state']!='completed' for row in rows)
            value['timeout_broker_context']={'requests':len(rows),'noncompleted':noncompleted,'recovered_transport':recovered}
            # D48 (policy D14, 2026-09-19): budget exhaustion is a Candidate outcome that
            # is scored, never voided.  This rule used to rewrite the candidate_timeout
            # above into provider_infrastructure_error whenever OUR OWN 600 s case
            # deadline caught a request in flight -- and the cut request is always the
            # last row of the window, so D13's recovered-transport carve-out can never
            # cover it.  The evaluator ending the case is not a provider failure.
            # One product process is killed, so at most one request can be caught in
            # flight; anything beyond that is still an unexplained provider result.
            if noncompleted>recovered+1 or (noncompleted>recovered and not any(row['state']=='completed' for row in rows)):
                value.update(classification='provider_infrastructure_error',error='Case deadline interrupted a pending/unknown provider result; no resampling permitted')
            elif noncompleted>recovered:
                value.update(case_budget_exhausted=True,
                    error='600 second aggregate case deadline exceeded; the last in-flight request was cut by the evaluator own deadline')
        except Exception as exc:
            value.update(classification='evaluator_infrastructure_error',error='Cannot establish provider completion at case timeout: '+type(exc).__name__)
    elif completed.returncode and value.get("classification") not in {"candidate_agent_failure", "candidate_artifact_missing", "candidate_capability_gap"}:
        value.update(classification="evaluator_infrastructure_error", error="case driver exited without a valid terminal result",
                     driver_exit_code=completed.returncode, driver_stderr=completed.stderr[-2000:])
    write_json(record_path, value)
    return value


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


def _run_case(
    repository: Path, prompt: Path, output: Path, *, broker_endpoint: str,
    execute: bool = False, python_executable: str | None = None,
) -> dict[str, Any]:
    repository = repository.resolve()
    prompt = prompt.resolve()
    output = output.resolve()
    runtime_python = python_executable or os.environ.get("DEEPTUTOR_PYTHON", sys.executable)
    runtime_root = output / "runtime"
    for name in ("home", "cache", "tmp", "config", "deeptutor-home"):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    deeptutor_home = runtime_root / "deeptutor-home"
    catalog_path = _write_model_catalog(deeptutor_home, broker_endpoint)
    surface = _audit_public_tool_surface(repository, output)
    evaluator_adapter_path = Path(__file__).resolve().parent
    adapter_path = runtime_root / "transport_adapter"
    adapter_path.mkdir(parents=True, exist_ok=True)
    for name in ("sitecustomize.py", "responses_client_adapter.py", "transport_bootstrap.py"):
        shutil.copyfile(evaluator_adapter_path / name, adapter_path / name)
    argv = command(repository, prompt, output, runtime_python)
    # Do not inherit evaluator credentials or unrelated host state.  The real
    # lower product receives only the broker placeholder and a case-local home.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    env.update(
        {
            # sitecustomize.py installs the credential-free Chat -> Responses
            # adapter before DeepTutor imports its production pipeline.
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(adapter_path), str(repository), env.get("PYTHONPATH", "")) if part
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(runtime_root / "home"),
            "XDG_CACHE_HOME": str(runtime_root / "cache"),
            "TMPDIR": str(runtime_root / "tmp"),
            "XDG_CONFIG_HOME": str(runtime_root / "config"),
            "DEEPTUTOR_HOME": str(runtime_root / "deeptutor-home"),
            "OPENAI_API_KEY": PLACEHOLDER_TOKEN,
            "OPENAI_BASE_URL": _chat_base_url(broker_endpoint),
            "AGENTSWE_RESPONSES_BASE_URL": broker_endpoint,
            "DEEPTUTOR_MODEL": LOWER_MODEL,
            "AGENTSWE_REQUIRED_MODEL": LOWER_MODEL,
            "AGENTSWE_REQUIRED_REASONING_EFFORT": LOWER_EFFORT,
            # The model/product must author this file in its own writable
            # runtime workspace.  The evaluator copies it only after the
            # product exits; it never materializes a replacement artifact.
            "DEEPTUTOR_AGENT_RESULT": str(repository / "agent_result.json"),
            "DEEPTUTOR_CASE_ID": os.environ.get("DEEPTUTOR_CASE_ID") or output.name,
            "DEEPTUTOR_RUNTIME_NONCE": os.environ.get("DEEPTUTOR_RUNTIME_NONCE", ""),
            "AGENTSWE_RESPONSES_ADAPTER": "1",
            "AGENTSWE_ADAPTER_MARKER": str(runtime_root / "responses_adapter_installed.json"),
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
    )
    record: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-lower-launch/v1",
        "entry": "deeptutor.capabilities.mastery.capability.MasteryPathCapability.run",
        "command": argv,
        "model": LOWER_MODEL,
        "reasoning_effort": LOWER_EFFORT,
        "transport": "evaluator-owned-responses-broker",
        "candidate_credential": PLACEHOLDER_TOKEN,
        "broker_endpoint": broker_endpoint,
        "chat_base_url": _chat_base_url(broker_endpoint),
        "model_catalog_path": str(catalog_path),
        "case_id": os.environ.get("DEEPTUTOR_CASE_ID") or output.name,
        "runtime_nonce": os.environ.get("DEEPTUTOR_RUNTIME_NONCE", ""),
        "artifact_contract": {
            "path": str(repository / "agent_result.json"),
            "owner": "lower_agent_product",
            "required_fields": ["schema_version", "case_id", "status", "summary", "artifacts"],
            "evaluator_may_synthesize": False,
        },
        "public_tool_surface": surface,
        "execution_requested": execute,
        "executed": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    product_artifact = repository / "agent_result.json"
    product_artifact.unlink(missing_ok=True)
    write_json(output / "launcher_result.json", record)
    if not execute:
        return record

    # The trusted driver produced this preflight inside the shared case scope.
    runtime_record = json.loads((output / "runtime_probe.json").read_text())
    record["runtime_probe"] = runtime_record
    if runtime_record.get("infra_valid") is not True:
        record.update(
            {
                "classification": runtime_record.get(
                    "classification", "runtime_dependency_infrastructure_error"
                ),
                "error": "; ".join(str(item) for item in runtime_record.get("errors", [])),
                "product_terminal_failure": False,
            }
        )
        write_json(output / "launcher_result.json", record)
        return record
    attribution_path = output / 'import_attribution.json'
    if attribution_path.is_file():
        attribution = json.loads(attribution_path.read_text())
        record['import_attribution'] = attribution
        if attribution.get('valid') is True:
            record.update(classification='candidate_build_failure', error=attribution['reason'],
                import_execution_attempted=True, executed=False, exit_code=1,
                product_terminal_failure=True, failure_phase='product_import')
            write_json(output / 'launcher_result.json', record)
            return record
    runtime_capability_gap = runtime_record.get("classification") == "candidate_capability_gap"
    fixture_root = output / "fixture"
    fixture_script = evaluator_adapter_path / "case_fixture.py"
    fixture_command = [runtime_python, "-I", str(fixture_script), "--repository", str(repository),
        "--case-id", record["case_id"], "--output", str(fixture_root)]
    fixture_env = {key: env[key] for key in ("PATH", "LANG", "LC_ALL", "HOME", "DEEPTUTOR_HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE") if key in env}
    fixture_env["AGENTSWE_FIXTURE_RUNTIME_ROOT"] = str(runtime_root)
    prepared = subprocess.run(fixture_command, cwd="/", env=fixture_env, text=True, capture_output=True,
        timeout=90, check=False)
    if prepared.returncode != 0:
        failure_path = fixture_root / "fixture-failure.json"
        failure = json.loads(failure_path.read_text()) if failure_path.is_file() else {}
        record.update({"classification": failure.get("classification", "evaluator_infrastructure_error"),
            "fixture_ready": False, "fixture_failure": failure, "product_terminal_failure": True,
            "error": failure.get("reason", prepared.stderr[-1000:])})
        write_json(output / "launcher_result.json", record)
        return record
    context = json.loads((fixture_root / "fixture-context.json").read_text())
    env["DEEPTUTOR_RUNTIME_NONCE"] = context["runtime_nonce"]
    record["runtime_nonce"] = context["runtime_nonce"]
    executed_prompt = output / "executed_task.md"
    executed_prompt.write_text(prompt.read_text(encoding="utf-8") + "\n\n## Observed incident and existing learner context\n\n"
        + json.dumps(context, indent=2, ensure_ascii=False) + "\n\nAct as the authorized operator of this existing learner path. Do not replace it with a lesson about the incident. Use registered product tools and report their actual observations.\n", encoding="utf-8")
    argv = command(repository, executed_prompt, output, runtime_python, session_id=context["session_id"])
    import hashlib
    from protocol import tree_digest
    import importlib.util
    if '@@AGENTSWE_EDITING_CONTROL@@' not in sys.path:sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
    digest_spec=importlib.util.spec_from_file_location('deeptutor_source_digest','@@AGENTSWE_EDITING_CONTROL@@/validate_formal_config.py')
    digest_module=importlib.util.module_from_spec(digest_spec);digest_spec.loader.exec_module(digest_module)
    logical_context = {"task_source_digest":digest_module.tree_digest(Path(__file__).resolve().parents[1]),"candidate_digest":tree_digest(repository),"case_id":record["case_id"],
        "runtime_nonce":context["runtime_nonce"],"output_path":str(output.resolve()),
        "executed_task_sha256":hashlib.sha256(executed_prompt.read_bytes()).hexdigest(),
        "harness_source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    logical_context["context_id"]=hashlib.sha256(json.dumps(logical_context,sort_keys=True).encode()).hexdigest()
    record["logical_context_id"] = logical_context["context_id"]
    write_json(output / "logical-context.json",logical_context)
    relay = UnixHTTPRelay(broker_endpoint,context_id=logical_context["context_id"])
    env["AGENTSWE_BROKER_UDS"] = "/run/agentswe/lower.sock"
    env["AGENTSWE_TRANSPORT_PREFLIGHT"] = str(runtime_root / "transport_preflight.json")
    argv = [runtime_python, str(adapter_path / "transport_bootstrap.py"), *argv[1:]]
    try:
        argv, sandbox = _sandbox_command(argv, repository, runtime_root, runtime_python, relay_socket=relay.path)
    except Exception:
        relay.close()
        raise
    sandbox.update({"network_namespace": "isolated", "host_tcp_network_access": False,
        "network_egress": "case-scoped fixed lower Responses UDS relay", "etc_mount": "localtime,hosts,nsswitch.conf only"})
    record.update({"command": argv, "sandbox": sandbox, "fixture_ready": True,
        "fixture_context": str(fixture_root / "fixture-context.json"), "executed_task_path": str(executed_prompt),
        "bound_mastery_path_id": context["path_id"]})
    try:
        record["executed"] = True
        write_json(output / "launcher_result.json", record)
        completed = subprocess.run(
            argv,
            cwd=repository,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        record.update({"classification": "candidate_timeout", "timed_out": True, "error": str(exc)})
        write_json(output / "launcher_result.json", record)
        return record
    except OSError as exc:
        record.update({"classification": "launcher_infrastructure_error", "error": str(exc)})
        write_json(output / "launcher_result.json", record)
        return record
    finally:
        write_json(output / "transport_relay_events.json", {"events": relay.events})
        relay.close()
    transport = runtime_root / "transport_preflight.json"
    record["transport_preflight"] = json.loads(transport.read_text()) if transport.is_file() else {"valid": False, "reason": "bootstrap did not attest its transport"}
    if record["transport_preflight"].get("valid") is not True:
        record.update({"classification": "evaluator_infrastructure_error", "executed": False,
            "error": "product transport preflight failed", "stderr_tail": completed.stderr[-2000:]})
        write_json(output / "launcher_result.json", record)
        return record
    parsed_events = _parse_json_lines(completed.stdout)
    observed_sessions = {event.get("session_id") for event in parsed_events if event.get("type") == "session"}
    record["observed_product_session_ids"] = sorted(str(item) for item in observed_sessions)
    trajectory_path = output / "trajectory.jsonl"
    trajectory_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in parsed_events),
        encoding="utf-8",
    )
    product_failed = completed.returncode != 0 or _product_output_failed(parsed_events)
    capability_gap = bool(surface.get("capability_gap")) or runtime_capability_gap
    if product_failed:
        classification = _failure_classification(completed.stderr, completed.stdout)
    elif capability_gap:
        classification = "candidate_capability_gap"
    else:
        classification = "candidate_valid"
    record.update(
        {
            "exit_code": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "product_terminal_failure": product_failed,
            "classification": classification,
            "product_capability_gap": capability_gap,
        }
    )
    product_artifact_written = product_artifact.is_file()
    if product_artifact_written:
        # Read/copy only after the real product process has exited.  This is
        # capture of a model/product-authored file, not evaluator synthesis.
        shutil.copy2(product_artifact, output / "agent_result.json")
    record.update({
        "product_artifact_path": str(product_artifact),
        "product_artifact_written": product_artifact_written,
        "artifact_owner": "lower_agent_product" if product_artifact_written else None,
        "evaluator_synthesized": False,
    })
    observed = subprocess.run([*fixture_command, "--observe"], cwd="/", env=fixture_env, text=True,
        capture_output=True, timeout=90, check=False)
    record["private_oracle_observation"] = str(fixture_root / "fixture-observation.json")
    record["oracle_observation_exit_code"] = observed.returncode
    marker = runtime_root / "responses_adapter_installed.json"
    if marker.is_file():
        shutil.copyfile(marker, output / "responses_adapter_installed.json")
    if not product_failed and not product_artifact_written:
        record.update({
            "classification": "candidate_artifact_missing",
            "product_capability_gap": True,
            "artifact_error": "DeepTutor product did not author agent_result.json",
        })
    if observed.returncode != 0:
        record.update({"classification": "evaluator_infrastructure_error", "error": "post-agent product observation failed"})
    if observed_sessions and observed_sessions != {context["session_id"]}:
        record.update({"classification": "evaluator_infrastructure_error", "error": "actual product session is not the seeded learner path"})
    # HTTP 429 from the evaluator's own broker is the disclosed per-case budget
    # refusing a further call (agentloop/broker.py:252 is its only producer), not
    # an endpoint or relay failure.  Counting it here classified every budget
    # stop as evaluator_infrastructure_error, which execution_evidence.py then
    # re-attributed to the Candidate; that is how test_001/test_002 of the 0919
    # formal run became Candidate zeros after 20s and 28s of a 600s case.  Real
    # transport breakage -- any other broker status, a relay error or a rejected
    # endpoint -- is still infrastructure.
    def _relay_transport_failure(item: dict[str, Any]) -> bool:
        if item.get("kind") in {"relay_error", "rejected_endpoint"}:
            return True
        return item.get("kind") == "broker_http_error" and item.get("status") != 429
    # The product exited by itself here (a timeout returned above), under a valid transport
    # preflight: a reply write the Candidate itself disconnected is not an evaluator failure.
    reply_disconnect = None
    if any(item.get("kind") == "relay_error" for item in relay.events):
        reply_disconnect = _candidate_reply_disconnect(relay.events, record["transport_preflight"],
                                                       _context_broker_rows(broker_endpoint, logical_context["context_id"]))
    if reply_disconnect is not None:
        record["candidate_reply_disconnect"] = reply_disconnect
    elif any(_relay_transport_failure(item) for item in relay.events):
        record.update({"classification": "evaluator_infrastructure_error", "error": "evaluator transport recorded an endpoint or relay failure"})
    write_json(output / "launcher_result.json", record)
    return record


# --- Candidate reply disconnect (2026-10-09) -----------------------------------------------
# The Candidate's own product may abort its model call after the broker already returned the
# complete answer; the relay's write of that reply then fails with BrokenPipe/ConnectionReset
# (the same shape as the public Lite smoke of 2026-10-09 on OpenWiki hidden test_001).  That
# disconnect is Candidate behaviour, not an evaluator transport failure.  It is tolerated only
# when all of these hold, and is then recorded in the launcher result:
#   * every relay_error is a reply-write disconnect: lower_transport.py read the broker's whole
#     HTTP 200 response before the write failed (phase reply_write, upstream_status 200);
#   * no other relay or broker event failed (no rejected endpoint, no broker HTTP error);
#   * the broker's ledger for this case context records every request completed (none
#     pending, unknown or failed), at least one per disconnect;
#   * the transport preflight is valid and the product exited by itself (the caller consults
#     this only on that path; a timeout keeps its own D48 rule).
_DISCONNECT_ERRORS = ("BrokenPipeError", "ConnectionResetError")


def _context_broker_rows(broker_endpoint: str, context_id: str) -> list[dict[str, Any]] | None:
    """This case context's logical requests in the broker ledger, or None when they cannot be read."""
    try:
        try:
            from .run_hidden import read_broker_stats
        except ImportError:
            from run_hidden import read_broker_stats
        stats = read_broker_stats(broker_endpoint)
        rows = [row for row in (stats.get("logical_requests") or {}).values()
                if isinstance(row, dict) and row.get("context_id") == context_id]
    except Exception:
        return None
    return rows


def _candidate_reply_disconnect(events: list[dict[str, Any]], transport_preflight: dict[str, Any],
                                broker_rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The Candidate's own disconnects on reply writes, or None when anything more failed."""
    if not isinstance(transport_preflight, dict) or transport_preflight.get("valid") is not True or broker_rows is None:
        return None
    disconnects = [item for item in events if item.get("kind") == "relay_error"]
    if not disconnects or any(item.get("phase") != "reply_write" or item.get("error_type") not in _DISCONNECT_ERRORS
                              or item.get("upstream_status") != 200 for item in disconnects):
        return None
    if any(item.get("kind") in {"rejected_endpoint", "broker_http_error"} for item in events):
        return None
    if len(broker_rows) < len(disconnects) or any(row.get("state") != "completed" for row in broker_rows):
        return None
    return {"package": "candidate-reply-disconnect", "classification_kept": "candidate",
            "product_exited_by_itself": True, "transport_preflight_valid": True,
            "tolerated_relay_events": [dict(item) for item in disconnects],
            "broker_context_requests": len(broker_rows), "broker_context_completed": len(broker_rows),
            "reason": "the Candidate product closed its own model connection after the broker had completed "
                      "the upstream call; the failed reply write is Candidate behaviour, not an evaluator "
                      "transport failure"}
# --- end Candidate reply disconnect ---------------------------------------------------------


def _parse_json_lines(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _product_output_failed(events: list[dict[str, Any]]) -> bool:
    """Treat a successful CLI process with a terminal error event as failure."""
    for value in events:
        if value.get("type") == "error":
            metadata = value.get("metadata")
            if isinstance(metadata, dict) and metadata.get("turn_terminal") is True:
                return True
            # A terminal error event from older product versions may not carry
            # the explicit fence, but must still have a final status.  Tool
            # results and arbitrary payload fields are deliberately ignored.
            if isinstance(metadata, dict) and metadata.get("status") in {"failed", "cancelled"}:
                return True
    return False


def _failure_classification(stderr: str, stdout: str) -> str:
    """Separate environment failures from a product/lower-agent failure."""
    text = f"{stderr}\n{stdout}".lower()
    if any(marker in text for marker in ("unsupported_endpoint", "evaluator_transport_", "evaluator_transport_preflight_failure")):
        return "evaluator_infrastructure_error"
    if "modulenotfounderror" in text or "importerror" in text:
        return "runtime_dependency_infrastructure_error"
    if "provider_infrastructure_error" in text:
        return "provider_infrastructure_error"
    if any(
        marker in text
        for marker in (
            "connection refused",
            "urlopen error",
            "broker_error",
            "candidate_placeholder_required",
        )
    ):
        return "broker_infrastructure_error"
    return "candidate_agent_failure"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--source-repository",type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--python", dest="python_executable")
    args = parser.parse_args()
    print(json.dumps(run(**vars(args)), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
