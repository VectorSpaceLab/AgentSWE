#!/usr/bin/env python3
"""Run the production OpenWiki CLI through an evaluator-owned broker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from ..protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, make_tree_writable, tree_digest, write_json
except ImportError:
    from agentloop.protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, make_tree_writable, tree_digest, write_json


ROOT = Path(__file__).resolve().parents[2]
from agentloop.evaluator.transport_sandbox import FixedLowerRelay, sandbox_command, load_transport_health
from agentloop.owned_resources import run_owned
from agentloop.evaluator.execution_evidence import native_artifact_path, broker_observation_reference
from agentloop.evaluator.process_observation import run_observed, load_observation, load_product_start
TASK_NODE = (ROOT / ".runtime" / "node-v22.12.0-linux-x64" / "bin" / "node").resolve()
TASK_NODE_VERSION = "v22.12.0"


def command(repo: Path, request: Path, node: Path = TASK_NODE) -> list[str]:
    entry = repo / "dist" / "cli.js"
    if not entry.is_file():
        raise FileNotFoundError(f"compiled OpenWiki entry missing: {entry}")
    if not node.is_file():
        raise FileNotFoundError(f"task-local Node 22 runtime missing: {node}")
    task = request.read_text(encoding="utf-8")
    return [str(node), str(entry), "--update", "--print", "--modelId", LOWER_MODEL, task]


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _stats_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid evaluator broker endpoint: {endpoint!r}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/stats", "", ""))


def read_broker_stats(endpoint: str) -> dict[str, object]:
    with urllib.request.urlopen(_stats_url(endpoint), timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats response must be an object")
    if value.get("schema_version") != 2:
        raise ValueError("broker stats schema is not locked to version 2")
    if value.get("model") != LOWER_MODEL or value.get("reasoning_effort") != LOWER_EFFORT:
        raise ValueError("broker protocol does not match the locked lower model")
    if not value.get("broker_instance_id"):
        raise ValueError("broker stats do not identify an evaluator broker instance")
    if value.get("credential_value_recorded") is not False:
        raise ValueError("broker stats must not record credential values")
    return value


def _stats_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, int]:
    if before.get("broker_instance_id") != after.get("broker_instance_id"):
        raise ValueError("broker instance changed during lower-agent case")
    fields = (
        "calls", "successful_calls", "failures", "broker_failures",
        "provider_failures", "credential_failures", "prompt_tokens",
        "completion_tokens", "total_tokens", "forced_overrides",
    )
    delta = {field: int(after.get(field, 0) or 0) - int(before.get(field, 0) or 0) for field in fields}
    if any(value < 0 for value in delta.values()):
        raise ValueError("broker counters moved backwards")
    return delta


# --- 122 (2026-09-24) deadline-kill attribution -----------------------------------------
# A product killed at the case deadline (D48 timeout branch) may leave ONE lower request in
# flight.  (b) Settle it before reading broker stats: the broker reports token totals as null
# while a request is pending, and _stats_delta reads null as 0 ("counters moved backwards",
# 0922-tc-v1-001-openwiki hidden test_004).  (a) The exchange the kill cut is a Candidate
# timeout artefact, not an evaluator transport failure (hidden test_001: BrokenPipeError on
# the reply write).  Same policy as D48/D49 and tools/patch_openhands_deadline_kill_scoring.py.
_D122_PENDING_WAIT_SECONDS = 30.0  # inside _inner_case_deadline's 45 s seal margin
_D122_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_D122_CUT_KINDS = ("broker_observation_incomplete", "broker_observation_store_incomplete",
                   "relay_evidence_incomplete")


def _d122_settled_broker_stats(endpoint: str, wait: float = _D122_PENDING_WAIT_SECONDS):
    started = time.monotonic()
    stats = read_broker_stats(endpoint)
    polls = 1
    while int(stats.get("pending_calls", 0) or 0) > 0 and time.monotonic() - started < wait:
        time.sleep(1.0)
        stats = read_broker_stats(endpoint)
        polls += 1
    return stats, {"package": "122", "wait_cap_seconds": wait, "polls": polls,
                   "waited_seconds": round(time.monotonic() - started, 3),
                   "pending_calls_after_wait": int(stats.get("pending_calls", 0) or 0)}


def _d122_timeout_stats_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, int]:
    pending = int(after.get("pending_calls", 0) or 0)
    if pending <= 0:
        return _stats_delta(before, after)
    unsettled = [field for field in _D122_TOKEN_FIELDS if after.get(field) is None]
    delta = _stats_delta(before, {**after, **{field: before.get(field, 0) for field in unsettled}})
    if unsettled:
        delta["usage_unsettled_pending_calls"] = pending
    return delta


def _d122_deadline_kill_in_flight(value: dict, relay_errors: list, observed_transport: dict,
                                  observation: dict) -> dict | None:
    """The one exchange a deadline kill cut, or None when the defects are anything more."""
    if (value.get("classification") != "candidate_timeout"
            or value.get("candidate_classification") != "candidate_timeout"
            or value.get("infrastructure_invalid") or value.get("product_started") is not True):
        return None
    if not observed_transport.get("valid") or observed_transport.get("endpoint_mapping_errors"):
        return None
    in_flight = int(value.get("broker_calls", 0) or 0) - int(value.get("broker_successful_calls", 0) or 0)
    if in_flight not in (0, 1) or (value.get("broker_delta") or {}).get("failures", 0):
        return None
    cut = [e for e in relay_errors if not (isinstance(e, dict) and e.get("error") in _D122_CUT_KINDS)]
    incomplete = [e for e in relay_errors if isinstance(e, dict) and e.get("error") == "broker_observation_incomplete"]
    relay = [e for e in relay_errors if isinstance(e, dict) and e.get("error") == "relay_evidence_incomplete"]
    store = [e for e in relay_errors if isinstance(e, dict) and e.get("error") == "broker_observation_store_incomplete"]
    if len(cut) > 1 or len(incomplete) > 1 or len(relay) > 1 or len(store) > 1:
        return None
    if cut and not (isinstance(cut[0], dict) and _d13_transport_error(str(cut[0].get("error")))):
        return None
    if incomplete and not all(_d13_transport_error(str(r)) for r in incomplete[0].get("reasons") or ["?"]):
        return None
    if relay and (int(relay[0].get("pending_handlers", 0) or 0) > 1 or relay[0].get("dropped_events")):
        return None
    if int(observation.get("relay_dropped_events", 0) or 0) or int(observation.get("pending", 0) or 0) > 1:
        return None
    if not relay_errors and in_flight == 0:
        return None
    return {"package": "122", "classification_kept": "candidate_timeout",
            "in_flight_calls_at_read": in_flight, "tolerated_transport_errors": list(relay_errors),
            "reason": "the case deadline killed the product with one lower request in flight; the "
                      "cut exchange is part of the Candidate timeout, not an evaluator transport failure"}
# --- end 122 ------------------------------------------------------------------------------


# --- Candidate reply disconnect (2026-10-09) -----------------------------------------------
# Next to 122, for a product that exited by itself: the Candidate's own code may abort its
# model call after the broker already holds the complete answer, and the relay's write of
# that reply then fails with BrokenPipe/ConnectionReset (public Lite smoke 2026-10-09,
# OpenWiki hidden test_001: the product's client timeout ended its /chat/completions call
# after 30 s, the broker completed it at 34.05 s with finish_reason stop, and the product
# exited on its own after 49 s of a 590 s case).  That disconnect is Candidate behaviour,
# not an evaluator transport failure.  It is tolerated only when all of these hold:
#   * the relay errors are such disconnects on a model path, plus the one incomplete
#     observation each of them leaves (naming only that error) and the store-level
#     incomplete marker; nothing else;
#   * for each disconnected request the relay's broker observation holds the complete
#     upstream answer (HTTP 200, terminal model status), the case window's broker ledger
#     rows are all completed, and the broker counters show no failure and nothing in flight;
#   * the product exited by itself (the caller consults this only on that path; a deadline
#     kill stays with 122) and the transport preflight is valid.
# A BrokenPipe/ConnectionReset after a 200 response was read can only come from the reply
# write: the relay reads the broker's whole response before it writes to the product.
_DISCONNECT_ERRORS = ("BrokenPipeError", "ConnectionResetError")


def _disconnect_observation(observation: dict, reference: dict) -> dict:
    path = reference.get("observation_path")
    for record in observation.get("records") or []:
        if isinstance(record, dict) and path and record.get("path") == path:
            return record
    return {}


def _candidate_reply_disconnect(relay_errors: list, transport: dict, observation: dict,
                                broker_before: dict, broker_after: dict, broker: dict) -> dict | None:
    """The Candidate's own disconnects on reply writes, or None when the defects are anything more."""
    if not relay_errors or not transport.get("valid") or transport.get("endpoint_mapping_errors"):
        return None
    if not all(isinstance(e, dict) for e in relay_errors):
        return None
    disconnects = [e for e in relay_errors if e.get("error") in _DISCONNECT_ERRORS]
    incomplete = [e for e in relay_errors if e.get("error") == "broker_observation_incomplete"]
    store = [e for e in relay_errors if e.get("error") == "broker_observation_store_incomplete"]
    if (not disconnects or len(disconnects) + len(incomplete) + len(store) != len(relay_errors)
            or len(incomplete) != len(disconnects) or len(store) > 1
            or any(e.get("path") not in ("/v1/responses", "/v1/chat/completions") for e in disconnects)):
        return None
    if (observation.get("errors") or int(observation.get("pending", 0) or 0)
            or int(observation.get("relay_pending_handlers", 0) or 0)
            or int(observation.get("relay_dropped_events", 0) or 0)):
        return None
    requests = []
    for reference in incomplete:
        record = _disconnect_observation(observation, reference)
        reasons = reference.get("reasons") or []
        if (len(reasons) != 1 or reasons[0] not in _DISCONNECT_ERRORS or record.get("errors") != reasons
                or record.get("response_status") != 200 or record.get("model_status") != "terminal"
                or reference.get("path") not in {e.get("path") for e in disconnects}):
            return None
        started, finished = record.get("started_at"), record.get("finished_at")
        requests.append({"path": reference.get("path"), "observation_path": reference.get("observation_path"),
                         "error": reasons[0], "response_status": 200, "model_status": "terminal",
                         "choice_finish_reasons": record.get("choice_finish_reasons"),
                         "upstream_seconds": round(finished - started, 3)
                         if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None})
    # every other exchange of the case was observed completely
    tolerated_paths = {item["observation_path"] for item in requests}
    if any(isinstance(r, dict) and r.get("complete") is not True and r.get("path") not in tolerated_paths
           for r in observation.get("records") or []):
        return None
    if (any(int(broker.get(field, 0) or 0) for field in ("failures", "broker_failures", "provider_failures",
                                                          "credential_failures"))
            or int(broker.get("calls", 0) or 0) != int(broker.get("successful_calls", 0) or 0)
            or int(broker.get("successful_calls", 0) or 0) < len(requests)
            or int(broker_after.get("pending_calls", 0) or 0)):
        return None
    before_rows = broker_before.get("requests") if isinstance(broker_before.get("requests"), list) else []
    after_rows = broker_after.get("requests") if isinstance(broker_after.get("requests"), list) else None
    if after_rows is not None:
        window = after_rows[len(before_rows):]
        if len(window) < len(requests) or any(not isinstance(row, dict) or row.get("ok") is not True
                                              or row.get("upstream_completion") != "completed" for row in window):
            return None
    return {"package": "candidate-reply-disconnect", "classification_kept": "candidate",
            "product_exited_by_itself": True, "transport_preflight_valid": True,
            "tolerated_transport_errors": list(relay_errors), "disconnected_requests": requests,
            "reason": "the Candidate product closed its own model connection after the broker had completed "
                      "the upstream call; the failed reply write is Candidate behaviour, not an evaluator "
                      "transport failure"}


def _self_exit_transport_verdict(transport: dict, relay_errors: list, observation: dict, broker_before: dict,
                                 broker_after: dict, broker: dict) -> tuple[str | None, dict | None]:
    """Transport verdict for a product that exited by itself: ('evaluator_failure', None) for an invalid preflight,
    an endpoint mapping error or any relay error, except (None, record) for a tolerated Candidate reply disconnect."""
    disconnect = _candidate_reply_disconnect(relay_errors, transport, observation, broker_before, broker_after, broker)
    if not transport.get("valid") or transport.get("endpoint_mapping_errors") or (relay_errors and disconnect is None):
        return "evaluator_failure", None
    return None, disconnect
# --- end Candidate reply disconnect ---------------------------------------------------------


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


def _broker_classification(delta: dict[str, int], tolerated: int = 0) -> str | None:
    """`tolerated` is the D13 count of recovered upstream transport failures in this delta;
    they are noise, so they are subtracted before the provider/broker attribution."""
    if delta.get("credential_failures", 0):
        return "credential_mount_failure"
    if delta.get("provider_failures", 0) > tolerated:
        return "provider_failure"
    if delta.get("broker_failures", 0):
        return "broker_failure"
    if delta.get("failures", 0) > tolerated:
        return "broker_failure"
    return None


def _candidate_environment(output: Path, endpoint: str, workspace: Path, case_id: str) -> dict[str, str]:
    """Build a minimal environment with no inherited provider credentials."""
    home = output / "candidate-home"
    temp = output / "candidate-tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    env = {
        # The product is intentionally pinned to the evaluator-prepared Node 22
        # runtime.  Native addons must never inherit whichever Node happens to
        # be first on the host PATH.
        "PATH": os.pathsep.join((str(TASK_NODE.parent), "/usr/bin", "/bin")),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "OPENWIKI_PROVIDER": "openai",
        "OPENWIKI_MODEL_ID": LOWER_MODEL,
        "OPENAI_BASE_URL": endpoint.removesuffix("/responses"),
        "OPENAI_API_KEY": PLACEHOLDER_KEY,
        "AGENTSWE_REQUIRED_MODEL": LOWER_MODEL,
        "AGENTSWE_REQUIRED_REASONING_EFFORT": LOWER_EFFORT,
        "OPENWIKI_AGENT_RESULT": str(workspace / "agent_result.json"),
        "OPENWIKI_CASE_ID": case_id,
        "OPENWIKI_DISABLE_TELEMETRY": "1",
    }
    return env


def _better_sqlite3_package(repo: Path) -> Path | None:
    """Locate the real pnpm package directory without assuming a root link."""
    direct = repo / "node_modules" / "better-sqlite3"
    if direct.is_dir():
        return direct.resolve()
    matches = sorted(
        (repo / "node_modules" / ".pnpm").glob(
            "better-sqlite3@*/node_modules/better-sqlite3"
        )
    )
    return matches[0].resolve() if matches else None


def runtime_preflight(repo: Path, output: Path, timeout: int = 60) -> dict[str, object]:
    """Prove Node 22 and the SQLite native addon before any broker call.

    This is evaluator/runtime validation, not Candidate behavior.  The probe
    opens an in-memory database, performs a write/read round trip, and closes
    it; a bare ``require()`` is too weak to establish that the addon works.
    """
    repo=repo.resolve()
    stdout_path = output / "runtime-preflight.stdout.log"
    stderr_path = output / "runtime-preflight.stderr.log"
    value: dict[str, object] = {
        "schema_version": "openwiki-runtime-preflight/v1",
        "owner": "evaluator_runtime",
        "phase": "pre_broker_product_entry",
        "node_path": str(TASK_NODE),
        "required_node_version": TASK_NODE_VERSION,
        "product_started": False,
        "broker_calls": 0,
        "credential_mounted_to_candidate": False,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    package = _better_sqlite3_package(repo)
    value["better_sqlite3_package"] = str(package) if package else None
    if not TASK_NODE.is_file() or package is None:
        value.update(
            valid=False,
            classification="native_binding_failure",
            failure="task_local_runtime_or_package_missing",
        )
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(
            "task-local Node 22 runtime or better-sqlite3 package is missing\n",
            encoding="utf-8",
        )
        write_json(output / "runtime-preflight.json", value)
        return value

    script = r"""
const path = require('node:path');
const packagePath = path.resolve(process.argv[1]);
const Database = require(packagePath);
const db = new Database(':memory:');
db.exec("CREATE TABLE runtime_probe (value TEXT NOT NULL); INSERT INTO runtime_probe VALUES ('node22-native-ok')");
const row = db.prepare('SELECT value FROM runtime_probe').get();
db.close();
process.stdout.write(JSON.stringify({
  node: process.version,
  modules: process.versions.modules,
  sqlite: row.value,
  closed: !db.open,
  packagePath,
}) + '\n');
"""
    try:
        process, resources = run_owned(
            sandbox_command([str(TASK_NODE), "-e", script, str(package)],
                readonly=(repo, TASK_NODE.parent.parent), cwd=repo),
            cwd=repo,
            env={
                "PATH": os.pathsep.join((str(TASK_NODE.parent), "/usr/bin", "/bin")),
                "HOME": str(output / "runtime-home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            output=output / 'preflight_resources',
            timeout=timeout,
        )
        value['preflight_resource_contract'] = resources
        stdout_path.write_text(process.stdout, encoding="utf-8")
        stderr_path.write_text(process.stderr, encoding="utf-8")
        details = json.loads(process.stdout) if process.stdout.strip() else {}
        valid = (
            process.returncode == 0
            and isinstance(details, dict)
            and details.get("node") == TASK_NODE_VERSION
            and details.get("sqlite") == "node22-native-ok"
            and details.get("closed") is True
        )
        binding = package / "build" / "Release" / "better_sqlite3.node"
        value.update(
            valid=valid,
            classification="runtime_ready" if valid else "native_binding_failure",
            failure=None if valid else "native_database_round_trip_failed",
            exit_code=process.returncode,
            observed=details,
            binding_path=str(binding),
            binding_sha256=_sha256(binding),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        value.update(
            valid=False,
            classification="native_binding_failure",
            failure=f"{type(exc).__name__}",
        )
    write_json(output / "runtime-preflight.json", value)
    return value


def _runtime_failure(output: Path, preflight: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        "valid": False,
        "classification": "native_binding_failure",
        "infrastructure_invalid": True,
        "candidate_classification": None,
        "failure": preflight.get("failure", "native_binding_failure"),
        "product_started": False,
        "broker_calls": 0,
        "broker_successful_calls": 0,
        "broker_failures": 0,
        "runtime_preflight": str(output / "runtime-preflight.json"),
        "failure_attribution": {
            "owner": "evaluator_runtime",
            "phase": "pre_broker_product_entry",
            "infrastructure_classification": "native_binding_failure",
            "candidate_behavior_classification": None,
        },
    }
    value["trajectory"] = _write_trajectory(output, {
        "schema_version": "openwiki-agentloop-trajectory/v1",
        "events": [{
            "kind": "runtime_preflight_failure",
            "classification": "native_binding_failure",
            "owner": "evaluator_runtime",
            "product_started": False,
            "broker_calls": 0,
        }],
        "runtime_preflight": str(output / "runtime-preflight.json"),
        "agent_result": None,
    })
    write_json(output / "launcher_result.json", value)
    return value


def _result_from_process(case_id: str, process: subprocess.CompletedProcess[str], elapsed: float, workspace: Path) -> dict | None:
    authored = native_artifact_path(workspace)
    if authored.is_file():
        try:
            value = json.loads(authored.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    # The lower product, not the launcher, owns the terminal result contract.
    # Returning a synthesized result here would turn a missing product artifact
    # into a false positive.
    return None


def _contains_native_binding_error(*values: object) -> bool:
    text = "\n".join(str(value or "") for value in values).lower()
    return any(
        marker in text
        for marker in (
            "could not locate the bindings file",
            "better_sqlite3.node",
            "node_module_version",
            "err_dlopen_failed",
        )
    )


def _write_trajectory(output: Path, value: dict[str, object]) -> str:
    path = output / "trajectory.json"
    write_json(path, value)
    return str(path)


def _valid_agent_result(value: object, case_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == "openwiki-agent-result/v1"
        and value.get("case_id") == case_id
        and isinstance(value.get("observations"), list)
        and bool(value.get("observations"))
        and isinstance(value.get("integrity"), dict)
        and isinstance(value.get("decision"), dict)
        and isinstance(value["decision"].get("completion_claim"), str)
    )


def _broker_failure(output: Path, failure: str, exc: Exception, product_started: bool) -> dict:
    value = {
        "valid": False,
        "classification": "broker_failure",
        "infrastructure_invalid": True,
        "candidate_classification": None,
        "failure": failure,
        "error": f"{type(exc).__name__}: {exc}",
        "product_started": product_started,
        "broker_calls": 0,
        "broker_successful_calls": 0,
        "broker_failures": 0,
        "failure_attribution": {
            "infrastructure_classification": "broker_failure",
            "candidate_behavior_classification": None,
        },
    }
    value["trajectory"] = _write_trajectory(output, {
        "schema_version": "openwiki-agentloop-trajectory/v1",
        "events": [{"kind": "broker_failure", "failure": failure, "product_started": product_started}],
    })
    write_json(output / "launcher_result.json", value)
    return value


def _inner_case_deadline(remaining: float) -> float:
    """Product deadline strictly inside the owned scope deadline.

    ``run_owned`` arms the case scope with ``RuntimeMaxSec = remaining -
    cleanup_reserve``.  Handing the inner driver ``remaining`` makes its own
    deadline unreachable: the scope kills the driver first, so the inner timeout
    branch never writes launcher_result.json and its ``finally`` never seals
    ``native_broker_observation`` -- which ``_recover_owned_timeout`` then
    requires.  Reserve the same amount ``run_owned`` reserves, plus room for the
    timeout branch itself (broker stats read, workspace/product digests, relay
    close and the observation seal), so a case that exhausts its budget is
    always sealed and attributable instead of evaluator-invalid.
    """
    scope_reserve = min(10.0, max(.25, remaining * .1))
    # 45 s (was 20): the driver's own in-scope setup and the timeout branch's bookkeeping
    # must both fit before the scope deadline (formal test_004, 2026-09-20: 21 s of setup
    # alone pushed the product past the scope kill).
    seal_margin = min(45.0, max(1.0, remaining * .1))
    return max(.001, remaining - scope_reserve - seal_margin)


def _product_wait_budget(timeout: float, deadline_monotonic: "float | None") -> float:
    """Seconds the product may still run: until the absolute deadline the case owner armed
    (measured where the scope was armed, so the driver's own setup counts), else `timeout`."""
    if deadline_monotonic is None:
        return max(.001, float(timeout))
    return max(.001, min(float(timeout), float(deadline_monotonic) - time.monotonic()))


def run_case(repo: Path, request: Path, output: Path, endpoint: str, timeout: int = 600,
             working_directory: Path | None = None, *, case_setup_started: float | None = None,
             fixture_case_id: str | None = None, fixture_cases_root: Path | None = None, fixture_output: Path | None = None) -> dict:
    """Aggregate case budget shared by dev and hidden; Code/Result excluded."""
    setup_elapsed = max(0.0, time.monotonic() - case_setup_started) if case_setup_started else 0.0
    output.mkdir(parents=True, exist_ok=True)
    remaining = min(float(timeout), 600.0) - setup_elapsed
    if remaining <= 0:
        return _broker_failure(output, 'initial_fixture_exceeded_case_budget', RuntimeError('case setup consumed the total deadline'), False)
    command = [sys.executable, '-I', str(Path(__file__).with_name('resource_case_driver.py')),
        '--repository', str(repo.resolve()), '--request', str(request.resolve()), '--output', str(output.resolve()),
        '--endpoint', endpoint, '--timeout', str(_inner_case_deadline(remaining)),
        '--deadline-monotonic', str(time.monotonic() + _inner_case_deadline(remaining))]
    if working_directory:
        command += ['--working-directory', str(working_directory.resolve())]
    if fixture_case_id:
        if fixture_cases_root is None or fixture_output is None:raise ValueError('complete fixture descriptor required')
        command += ['--fixture-case-id',fixture_case_id,'--fixture-cases-root',str(fixture_cases_root.resolve()),'--fixture-output',str(fixture_output.resolve())]
    try:
        completed, resources = run_owned(command, cwd='/', env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            output=output / 'case_resources', timeout=remaining)
    except Exception as exc:
        return _broker_failure(output, 'case_resource_infrastructure', exc, False)
    path = output / 'launcher_result.json'
    try:
        original = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        value = json.loads(original) if original is not None else {'product_started': False}
        if not isinstance(value, dict):
            raise ValueError('launcher result is not an object')
        if original is not None and resources['timed_out']:
            with (output / 'launcher_result.before-aggregate-timeout.json').open('xb') as stream:
                stream.write(original)
    except (OSError, ValueError) as exc:
        value = {'product_started': False, 'infrastructure_invalid': True,
            'classification': 'evaluator_failure', 'failure': 'unreadable prior launcher result: ' + str(exc)}
    resources['initial_fixture_elapsed_seconds'] = setup_elapsed
    resources['configuration_delta'] = [] if timeout == 600 else [{'diagnostic_case_timeout_seconds': timeout, 'formal_default_seconds': 600}]
    value['case_resource_contract'] = resources
    if resources['timed_out']:
        value = _recover_owned_timeout(value, repo=repo, request=request, output=output,
            endpoint=endpoint, resources=resources)
    elif completed.returncode:
        value.update(valid=False, classification='evaluator_failure', infrastructure_invalid=True,
            failure='case resource driver failed', driver_stderr=completed.stderr[-2000:])
    write_json(path, value)
    return value


def _recover_owned_timeout(value: dict, *, repo: Path, request: Path, output: Path,
                           endpoint: str, resources: dict) -> dict:
    """Recover only an independently witnessed product start after outer kill.

    ``run_owned`` can terminate the case driver before its ``finally`` block
    seals launcher metadata.  A deterministic timeout may still be attributed
    to the Candidate when the evaluator independently proves the exact product
    execve, transport health, trusted resource scope, and a settled broker
    counter delta.  No wrapper start, successful broker count, or artifact is
    treated as a substitute for the product-start witness.
    """
    # An inner failure has precedence over the outer deadline.  A timeout
    # cannot turn a provider, transport, or native-runtime failure into a
    # Candidate result.
    if value.get('infrastructure_invalid') is True or value.get('classification') in {
            'provider_failure', 'credential_mount_failure', 'broker_failure',
            'launcher_failure', 'native_binding_failure', 'evaluator_failure', 'infrastructure_invalid'}:
        value.setdefault('timeout_start_recovered', False)
        return value
    value.update(valid=False, classification='evaluator_failure',
        candidate_classification=None, infrastructure_invalid=True,
        failure='aggregate timeout without independently verified product start')
    before = after = delta = None
    try:
        case_id = request.parent.name if request.name in {'input.md', 'request.json'} else request.stem.split('.', 1)[0]
        context = _validate_timeout_context(output, repo, request, case_id, resources)
        product = output / 'product'
        entry = product / 'dist' / 'cli.js'
        witness = load_product_start(output, context, product_entry=entry, product_executable=TASK_NODE)
        if witness['successful_execve']['argv'] != command(product, request):
            raise ValueError('product start witness full request argv mismatch')
        health = load_transport_health(output, context['context_id'], endpoint=endpoint)
        preflight = json.loads((output / 'runtime-preflight.json').read_text())
        if preflight.get('valid') is not True:
            raise ValueError('runtime preflight was not valid before product start')
        before = _strict_broker_snapshot(json.loads((output / 'broker_before.json').read_text()))
        after = read_broker_stats(endpoint)
        after = _strict_broker_snapshot(after)
        write_json(output / 'broker_after.json', after)
        delta = _stats_delta(before, after)
        # A settled counter delta is necessary, but a failed, unknown, or
        # in-flight exchange remains evaluator infrastructure uncertainty.
        # D13: except a recovered upstream transport failure -- counted here for this case's
        # delta and, for the absolute ledger state below, across the whole ledger.
        recovered = _d13_recovered_transport_calls(before, after)
        recovered_total = _d13_recovered_transport_calls(None, after)
        if (_broker_classification(delta, recovered)
                or delta['calls'] - delta['successful_calls'] > recovered
                or after['protocol_failures'] != before['protocol_failures']):
            raise ValueError('aggregate timeout with unresolved provider execution')
        if any(after.get(key) != 0 for key in ('pending_calls', 'reserved_not_submitted_count')):
            raise ValueError('aggregate timeout with unresolved broker ledger state')
        if any(int(after.get(key) or 0) > recovered_total for key in ('unknown_calls', 'unknown_usage_calls')):
            raise ValueError('aggregate timeout with unresolved broker ledger state')
        from agentloop.evaluator.broker_observation import load_observations
        summary_path = output / 'native-broker-observation.json'
        sealed_reference = value.get('native_broker_observation', {})
        if (sealed_reference.get('path') != str(summary_path)
                or sealed_reference.get('sha256') != _sha256(summary_path)):
            raise ValueError('aggregate timeout without launcher-sealed broker reference')
        summary = load_observations(output, context['context_id'])
        if (summary.get('complete') is not True or summary.get('sealed') is not True
                or summary.get('pending') or len(summary.get('records', [])) < delta['calls']
                or any(record.get('complete') is not True for record in summary.get('records', []))):
            raise ValueError('aggregate timeout with unresolved transport delivery')
        transport = json.loads((output / 'transport_preflight.json').read_text())
        if (transport.get('valid') is not True or transport.get('endpoint_mapping_errors')
                or value.get('transport_errors')):
            raise ValueError('aggregate timeout without completed isolated transport evidence')
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, AttributeError) as exc:
        value['failure'] = str(exc)
        value['timeout_recovery'] = {'schema_version': 'openwiki-aggregate-timeout-recovery/v1',
            'verified': False, 'error': str(exc),
            'broker_after': str(output / 'broker_after.json') if after is not None else None}
        return value
    value.update(valid=False, classification='candidate_timeout', candidate_classification='candidate_timeout',
        infrastructure_invalid=False, failure='aggregate case deadline exceeded after verified product start',
        product_started=True, timeout_start_recovered=True,
        broker_calls=delta['calls'], broker_successful_calls=delta['successful_calls'],
        broker_failures=delta['failures'], provider_failures=delta['provider_failures'], broker_delta=delta,
        broker_after=str(output / 'broker_after.json'),
        product_start_observed=True,
        product_start_witness={'path': witness['path'], 'sha256': witness['sha256']},
        transport_health={'path': str(output / 'native-transport-health.json'),
                          'context_id': health['context_id']},
        timeout_recovery={'schema_version': 'openwiki-aggregate-timeout-recovery/v1',
                          'verified': True, 'product_start': witness['path'],
                          'transport_health': str(output / 'native-transport-health.json'),
                          'context_id': context['context_id'],
                          'delivery_complete': True,
                          'unknown_delta': 0, 'pending_after': 0})
    return value


def _strict_broker_snapshot(value: dict) -> dict:
    """Reject missing/null counters instead of converting them to zero."""
    if (not isinstance(value, dict) or value.get('schema_version') != 2
            or value.get('model') != LOWER_MODEL or value.get('reasoning_effort') != LOWER_EFFORT
            or not value.get('broker_instance_id') or value.get('credential_value_recorded') is not False):
        raise ValueError('broker stats schema is not locked to version 2')
    required = ('calls', 'successful_calls', 'failures', 'broker_failures',
                'provider_failures', 'credential_failures', 'protocol_failures', 'unknown_calls',
                'pending_calls', 'unknown_usage_calls', 'reserved_not_submitted_count',
                'prompt_tokens', 'completion_tokens', 'total_tokens', 'forced_overrides')
    for key in required:
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError('broker stats counter missing or null: ' + key)
    return value


def _validate_timeout_context(output: Path, repo: Path, request: Path, case_id: str, resources: dict) -> dict:
    from agentloop.evaluator.execution_evidence import validate_execution_context
    if (resources.get('schema_version') != 'agentswe-owned-case-resources/v1'
            or resources.get('valid') is not True or resources.get('timed_out') is not True
            or resources.get('cleanup', {}).get('complete') is not True
            or 'populated 0' not in resources.get('cleanup', {}).get('cgroup_events', '')
            or resources.get('memory_bytes') != 4096 * 1024 * 1024):
        raise ValueError('aggregate timeout resource evidence invalid')
    ownership = json.loads((output / 'case_resources' / 'scope-ownership.json').read_text())
    if any(ownership.get(key) != resources.get(key) for key in ('unit', 'description', 'cgroup', 'memory_bytes')):
        raise ValueError('aggregate timeout scope ownership mismatch')
    from agentloop.owned_resources import verify_identity
    verify_identity(resources.get('systemd_properties', {}), ownership)
    if resources.get('observed', {}).get('cgroup') != resources.get('cgroup'):
        raise ValueError('aggregate timeout observed cgroup mismatch')
    return validate_execution_context(output, repo, request, case_id, resources)


def _run_case(repo: Path, request: Path, output: Path, endpoint: str, timeout: int = 600, working_directory: Path | None = None,
              deadline_monotonic: "float | None" = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    preflight = json.loads((output / 'runtime-preflight.json').read_text())
    if not preflight.get("valid"):
        return _runtime_failure(output, preflight)
    try:
        broker_before = read_broker_stats(endpoint)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return _broker_failure(output, "broker_stats_before", exc, False)
    write_json(output / "broker_before.json", broker_before)

    workspace = output / "workspace"
    shutil.rmtree(workspace, ignore_errors=True)
    try:
        if working_directory and working_directory.is_dir():
            shutil.copytree(working_directory, workspace, symlinks=True)
        else:
            shutil.copytree(repo, workspace, symlinks=True)
        # The frozen Candidate remains read-only; this is an isolated,
        # disposable product workspace for the current case.
        make_tree_writable(workspace)
    except (OSError, shutil.Error) as exc:
        return _broker_failure(output, "workspace_materialization", exc, False)
    workspace_digest_before = tree_digest(workspace)
    product = output / "product"
    shutil.rmtree(product, ignore_errors=True)
    try:
        shutil.copytree(repo, product, symlinks=True)
        make_tree_writable(product)
    except (OSError, shutil.Error) as exc:
        return _broker_failure(output, "product_materialization", exc, False)
    product_digest_before = tree_digest(product)
    artifact_preexisting = native_artifact_path(workspace).exists()
    case_id = request.parent.name if request.name in {"input.md", "request.json"} else request.stem.split(".", 1)[0]
    env = _candidate_environment(output, endpoint, workspace, case_id)
    try:
        argv = command(product, request)
    except FileNotFoundError as exc:
        value = {
            "valid": False,
            "classification": "candidate_build_failure",
            "infrastructure_invalid": False,
            "failure": "lower_entry_missing",
            "error": f"{type(exc).__name__}: {exc}",
            "product_started": False,
            "broker_calls": 0,
            "broker_successful_calls": 0,
            "broker_failures": 0,
        }
        write_json(output / "launcher_result.json", value)
        return value
    write_json(output / "launcher_request.json", {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "transport": "evaluator-owned-broker", "candidate_credential": PLACEHOLDER_KEY, "credential_mounted_to_candidate": False, "entrypoint": "dist/cli.js", "node_runtime": str(TASK_NODE), "node_version": TASK_NODE_VERSION, "runtime_preflight": str(output / "runtime-preflight.json"), "product": str(product), "cwd": str(workspace), "argv": argv[:-1] + ["<case-request>"]})
    started = time.monotonic()
    import importlib.util
    if '@@AGENTSWE_EDITING_CONTROL@@' not in sys.path:sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
    digest_spec=importlib.util.spec_from_file_location('openwiki_source_digest','@@AGENTSWE_EDITING_CONTROL@@/validate_formal_config.py')
    digest_module=importlib.util.module_from_spec(digest_spec);digest_spec.loader.exec_module(digest_module)
    logical_context={'candidate_digest':tree_digest(product),'task_source_digest':digest_module.tree_digest(ROOT),
        'request_sha256':hashlib.sha256(request.read_bytes()).hexdigest(),'case_id':case_id,
        'product_entry_sha256':_sha256(product / 'dist/cli.js'),
        'output_path':str(output.resolve()),'workspace_path':str(workspace.resolve()),
        'resource_cgroup':Path('/proc/self/cgroup').read_text(),
        'remaining_case_timeout_seconds':timeout}
    logical_context['context_id']=hashlib.sha256(json.dumps(logical_context,sort_keys=True).encode()).hexdigest()
    write_json(output/'logical-context.json',logical_context)
    relay = FixedLowerRelay(endpoint,context_id=logical_context['context_id'], observation_dir=output).start()
    bootstrap = ['/usr/bin/python3', '/run/agentswe/transport.py', '--inside',
        '--uds', '/run/agentswe/lower.sock', '--preflight', str(Path(env['TMPDIR']) / 'transport_preflight.json'), '--', *argv]
    try:
        isolated_argv = sandbox_command(bootstrap,
            writable=(product, workspace, Path(env['HOME']), Path(env['TMPDIR'])),
            readonly=(TASK_NODE.parent.parent,), cwd=workspace, relay=relay)
    except Exception:
        relay.close()
        raise
    try:
        process = run_observed(isolated_argv, output=output, context=logical_context,
            cwd=workspace, env=env, timeout=_product_wait_budget(timeout, deadline_monotonic),
            product_entry=product / 'dist' / 'cli.js', product_executable=TASK_NODE)
    except subprocess.TimeoutExpired as exc:
        try:
            # 122 (b): settle the killed product's in-flight request (bounded) before reading.
            broker_after, _d122_settle = _d122_settled_broker_stats(endpoint)
            broker = _d122_timeout_stats_delta(broker_before, broker_after)
            write_json(output / "broker_after.json", broker_after)
        except Exception as stats_exc:
            return _broker_failure(output, "broker_stats_after_timeout", stats_exc, True)
        # D48 (policy D14): this branch IS the case deadline firing -- run_observed was
        # given _product_wait_budget() and the product was killed by the evaluator.  A
        # lower request in flight at that instant dies with its client and
        # native_broker.py:346 books it `failure_domain="provider"` unconditionally, so
        # _broker_classification below used to outrank candidate_timeout and void a case
        # that had spent its whole budget.  The cut request is always the last row of the
        # window, so D13's recovered-transport carve-out can never cover it.  One
        # product process is killed, so tolerate exactly one such row here and nowhere else.
        _d48_tolerated = _d13_recovered_transport_calls(broker_before, broker_after)
        if broker.get("successful_calls", 0) > 0:
            _d48_tolerated += 1
        infrastructure_classification = _broker_classification(broker, _d48_tolerated)
        classification = infrastructure_classification or "candidate_timeout"
        value = {"valid": False, "classification": classification, "infrastructure_invalid": infrastructure_classification is not None, "candidate_classification": "candidate_timeout", "entrypoint": "dist/cli.js", "stdout": str(exc.stdout or ""), "stderr": str(exc.stderr or ""), "product_started": True, "elapsed_seconds": round(time.monotonic() - started, 3), "broker_calls": broker["calls"], "broker_successful_calls": broker["successful_calls"], "broker_failures": broker["broker_failures"], "provider_failures": broker["provider_failures"], "broker_delta": broker, "runtime_preflight": str(output / "runtime-preflight.json"), "workspace_digest_before": workspace_digest_before, "workspace_digest_after": tree_digest(workspace), "product_digest_before": product_digest_before, "product_digest_after": tree_digest(product), "failure_attribution": {"broker_failures": broker["broker_failures"], "provider_failures": broker["provider_failures"], "infrastructure_classification": infrastructure_classification, "candidate_behavior_classification": "candidate_timeout"}}
        value["d122_broker_settle"] = _d122_settle
        # D49: this branch returns before the line further down that writes
        # stdout.log / stderr.log on the normal path, so a timed-out case used to leave
        # no captured product output at all -- the Builder never saw why it was killed
        # and `execution_evidence.py` cited a file that was never written.  The bytes are
        # already in hand (`run_observed` attaches the drained capture to the exception).
        for _name, _raw in (("stdout.log", exc.stdout), ("stderr.log", exc.stderr)):
            _text = _raw.decode("utf-8", "replace") if isinstance(_raw, (bytes, bytearray)) else str(_raw or "")
            (output / _name).write_text(_text, encoding="utf-8")
        value["trajectory"] = _write_trajectory(output, {
            "schema_version": "openwiki-agentloop-trajectory/v1",
            "events": [{"kind": "process_timeout", "entrypoint": "dist/cli.js", "elapsed_seconds": round(time.monotonic() - started, 3)}],
            "broker_before": str(output / "broker_before.json"),
            "broker_after": str(output / "broker_after.json"),
            "agent_result": None,
        })
        write_json(output / "launcher_result.json", value)
        return value
    except OSError as exc:
        try:
            broker_after = read_broker_stats(endpoint)
            broker = _stats_delta(broker_before, broker_after)
            write_json(output / "broker_after.json", broker_after)
        except Exception as stats_exc:
            return _broker_failure(output, "broker_stats_after_launcher_error", stats_exc, False)
        value = {
            "valid": False,
            "classification": "launcher_failure",
            "infrastructure_invalid": True,
            "candidate_classification": None,
            "failure": "process_spawn",
            "error": f"{type(exc).__name__}: {exc}",
            "product_started": False,
            "broker_calls": broker["calls"],
            "broker_successful_calls": broker["successful_calls"],
            "broker_failures": broker["broker_failures"],
            "provider_failures": broker["provider_failures"],
            "broker_delta": broker,
            "workspace_digest_before": workspace_digest_before,
            "workspace_digest_after": tree_digest(workspace),
            "product_digest_before": product_digest_before,
            "product_digest_after": tree_digest(product),
            "failure_attribution": {
                "infrastructure_classification": "launcher_failure",
                "candidate_behavior_classification": None,
            },
        }
        value["trajectory"] = _write_trajectory(output, {
            "schema_version": "openwiki-agentloop-trajectory/v1",
            "events": [{"kind": "launcher_failure", "failure": "process_spawn"}],
            "broker_before": str(output / "broker_before.json"),
            "broker_after": str(output / "broker_after.json"),
            "agent_result": None,
        })
        write_json(output / "launcher_result.json", value)
        return value
    finally:
        broker_observation = relay.close()
        write_json(output / 'native-broker-observation.json', broker_observation)
        broker_reference = broker_observation_reference(output, broker_observation)
        runtime_transport = Path(env['TMPDIR']) / 'transport_preflight.json'
        if runtime_transport.is_file():
            shutil.copyfile(runtime_transport, output / 'transport_preflight.json')
        if 'value' in locals():
            value['transport_errors'] = list(relay.errors)
            value['native_broker_observation'] = broker_reference
            value['broker_observations'] = broker_observation.get('record_references', [])
            try:
                observed_transport = json.loads(runtime_transport.read_text())
            except (OSError, ValueError):
                observed_transport = {}
            value['transport_preflight'] = observed_transport
            # 122 (a): the one exchange a deadline kill cut stays a Candidate timeout.
            _d122 = _d122_deadline_kill_in_flight(value, list(relay.errors), observed_transport,
                                                  broker_observation)
            if _d122 is not None:
                value['transport_errors'] = []
                value['d122_deadline_kill_in_flight'] = _d122
            elif (not observed_transport.get('valid') or observed_transport.get('endpoint_mapping_errors')
                    or relay.errors or (value.get('classification') == 'candidate_timeout'
                        and value.get('broker_calls', 0) > value.get('broker_successful_calls', 0))):
                value.update(classification='evaluator_failure', infrastructure_invalid=True,
                    failure='isolated transport invalid or unresolved in-flight broker request')
            write_json(output / 'launcher_result.json', value)

    workspace_digest_after = tree_digest(workspace)
    product_digest_after = tree_digest(product)

    (output / "stdout.log").write_text(process.stdout, encoding="utf-8")
    (output / "stderr.log").write_text(process.stderr, encoding="utf-8")
    result = _result_from_process(case_id, process, time.monotonic() - started, workspace)
    contract_valid = _valid_agent_result(result, case_id)
    try:
        broker_after = read_broker_stats(endpoint)
        broker = _stats_delta(broker_before, broker_after)
        write_json(output / "broker_after.json", broker_after)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return _broker_failure(output, "broker_stats_after", exc, True)

    infrastructure_classification = _broker_classification(
        broker, _d13_recovered_transport_calls(broker_before, broker_after))
    try:
        transport_preflight = json.loads((output / 'transport_preflight.json').read_text())
    except (OSError, ValueError):
        transport_preflight = {}
    # The product exited by itself here (a deadline kill returned above): a reply-write
    # disconnect the Candidate caused is not an evaluator transport failure.
    transport_failure, reply_disconnect = _self_exit_transport_verdict(
        transport_preflight, list(relay.errors), broker_observation, broker_before, broker_after, broker)
    if transport_failure:
        infrastructure_classification = transport_failure
    try:
        process_observation = load_observation(output, logical_context)
    except (OSError, ValueError) as exc:
        process_observation = {'complete': False, 'error': str(exc)}
    if process_observation.get('complete') is not True:
        infrastructure_classification = 'evaluator_failure'
    failure_detail = None
    if _contains_native_binding_error(process.stdout, process.stderr):
        candidate_classification = None
        infrastructure_classification = infrastructure_classification or "native_binding_failure"
        failure_detail = "native_binding_failure"
    elif broker["calls"] == 0:
        candidate_classification = "candidate_no_observable_behavior"
    elif not contract_valid:
        candidate_classification = "candidate_contract_failure"
    elif process.returncode != 0:
        candidate_classification = "candidate_product_failure"
    else:
        candidate_classification = "candidate_product_success"
    classification = infrastructure_classification or candidate_classification
    infrastructure_invalid = infrastructure_classification is not None
    artifact_path = native_artifact_path(workspace)
    artifact_provenance = {
        "artifact_path": str(artifact_path),
        "artifact_owner": "lower_agent_product",
        "producer_entry": "OpenWiki dist/cli.js product process",
        "evaluator_capture": str(output / "launcher_result.json"),
        "evaluator_synthesized": False,
        "exists": artifact_path.is_file(),
        "size_bytes": artifact_path.stat().st_size if artifact_path.is_file() else None,
        "sha256": _sha256(artifact_path),
    }
    value = {
        "valid": contract_valid and process.returncode == 0 and broker["successful_calls"] > 0 and broker["failures"] == 0,
        "classification": classification,
        "entrypoint": "dist/cli.js",
        "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "candidate_repo_digest": tree_digest(product),
        "workspace_digest": tree_digest(workspace),
        "agent_result": result,
        "artifact_provenance": artifact_provenance,
        "candidate_classification": candidate_classification,
        "failure": failure_detail,
        "infrastructure_invalid": infrastructure_invalid,
        "failure_attribution": {
            "broker_failures": broker["failures"],
            "broker_successful_calls": broker["successful_calls"],
            "owner": "evaluator_runtime" if infrastructure_classification == "native_binding_failure" else None,
            "infrastructure_classification": infrastructure_classification,
            "candidate_behavior_classification": candidate_classification,
        },
        "runtime_preflight": str(output / "runtime-preflight.json"),
        "product_entry_sha256": _sha256(product / "dist/cli.js"),
        "product_started": True,
        "artifact_preexisting": artifact_preexisting,
        "transport_preflight": transport_preflight,
        "transport_errors": [] if reply_disconnect is not None else relay.errors,
        "native_broker_observation": broker_reference,
        "broker_observations": broker_observation.get('record_references', []),
        "native_process_observation": process_observation,
        "sandbox": {"filesystem_isolated": True, "network_namespace_isolated": True,
            "host_tcp_reachable": False, "network_egress": "single fixed lower broker UDS"},
        "broker_calls": broker["calls"],
        "broker_successful_calls": broker["successful_calls"],
        "broker_failures": broker["broker_failures"],
        "provider_failures": broker["provider_failures"],
        "broker_delta": broker,
        "workspace_digest_before": workspace_digest_before,
        "workspace_digest_after": workspace_digest_after,
        "product_digest_before": product_digest_before,
        "product_digest_after": product_digest_after,
        "trajectory_path": str(output / "trajectory.json"),
        "trajectory_present": (output / "trajectory.json").is_file(),
    }
    if reply_disconnect is not None:
        # the tolerated relay errors stay on record here, as 122 keeps its own
        value["candidate_reply_disconnect"] = reply_disconnect
    value["trajectory"] = _write_trajectory(output, {
        "schema_version": "openwiki-agentloop-trajectory/v1",
        "events": [{
            "kind": "openwiki_cli",
            "entrypoint": "dist/cli.js",
            "exit_code": process.returncode,
            "elapsed_seconds": value["elapsed_seconds"],
            "classification": classification,
        }],
        "broker_before": str(output / "broker_before.json"),
        "broker_after": str(output / "broker_after.json"),
        "agent_result": str(artifact_path) if artifact_path.is_file() else None,
        "stdout": str(output / "stdout.log"),
        "stderr": str(output / "stderr.log"),
    })
    write_json(output / "launcher_result.json", value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-repository", type=Path, required=True)
    parser.add_argument("--case-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--working-directory", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    value = run_case(args.candidate_repository.resolve(), args.case_request.resolve(), args.output_dir.resolve(), args.broker_endpoint, args.timeout, args.working_directory.resolve() if args.working_directory else None)
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return 0 if value.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
