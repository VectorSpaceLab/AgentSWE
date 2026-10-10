#!/usr/bin/env python3
"""Layout facts shared by rejudge_case.py and refinalize_run.py.

The Edit siblings do not share one finalizer.  Three families exist, and they
differ in how the judge is invoked, where the per-case judging evidence lands,
what the "one logical request" guard is, and whether the finalizer will judge
cases beyond the one you care about.  Everything here is derived from the run
and the sibling tree on disk; nothing is hardcoded per task.

  shared_main        evaluator/formal_finalize.py -> `from formal_axes import main`
                     (deepcode, deeptutor, dyad).  Judging goes through
                     execution_scoring.judge_execution_case, so each case dir
                     carries scoring_intent.json and a bound contract is reused.
                     Independent per case: an invalid case blocks only itself.

  semantic_finalize  evaluator/formal_finalize.py -> `from semantic_finalize
                     import finalize` -> evaluator/shared_finalize.py, which
                     installs task-native adapters (find_case_record,
                     case_files, provenance_summary) into formal_axes_shared and
                     then calls the same main.  Same per-case evidence as above,
                     but records are BUILT by the tree's adapter, whose failures
                     are swallowed into an `evidence_preparation_error` record.

  standalone         the sibling owns the whole finalizer (claude, aider, codex,
                     openwiki).  It calls result_judge.py directly, so there is
                     no scoring_intent.json: the only no-resample guard is
                     result_judge.py refusing an output dir that already holds
                     logical_request_started.json or result_score_contract.json.
                     Claude's loop is FAIL-FAST: the first unpublishable case
                     aborts the run, so later cases were never judged and a
                     re-run judges all of them.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import hashlib
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SHARED_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")  # the shared Edit control plane (paper: 0905-edit-case-repair)
RESULT_JUDGE = SHARED_ROOT / "result_judge.py"
CONTROL_PYTHON = Path("/usr/bin/python3")
LAUNCH_CONTROL = Path("@@AGENTSWE_EDITING_RUNS@@/formal/launch_control")  # launch_formal_task.py records
CASES = tuple(f"test_{index:03d}" for index in range(1, 7))
# left in a tree's finalizer by patch_15_codex_finalizer_reuse.py
REUSE_GUARD_MARKER = "finalizer-reuse-guard"


class Refusal(RuntimeError):
    """The tool refuses to act; nothing has been changed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def pick_result_judge_broker(launches: list) -> Path:
    """Choose the formal Result-judge broker when a run owns several.

    Trees that give the public dev rounds their own judge broker leave two
    transport directories behind (ai-scientist: `judge-` and `public_judge-`).
    They are told apart by the container name the launch record carries -- the
    formal one names the result-judge role, the other names the dev/public one.
    """
    scored = []
    for path in launches:
        try:
            command = read_json(path)["command"]
            name = command[command.index("--name") + 1].lower()
        except (OSError, ValueError, KeyError, IndexError):
            continue
        stem = path.parent.name.lower()
        score = 0
        if "result-judge" in name or "result_judge" in name:
            score += 2
        if any(token in name for token in ("-dev-", "dev-judge", "public")):
            score -= 3
        if stem.startswith("public") or stem.startswith("dev"):
            score -= 3
        scored.append((score, path))
    if not scored:
        raise Refusal("several judge-broker launch records found and none is readable")
    best = max(score for score, _ in scored)
    winners = [path for score, path in scored if score == best]
    if len(winners) != 1:
        raise Refusal("several judge-broker launch records found and none names the formal "
                      "result-judge role; pass --broker-launch: "
                      + ", ".join(str(p) for p in launches))
    return winners[0]


def single_shot_finalizer(task_root: Path) -> bool:
    """True when the tree's finalizer refuses to reuse an existing case directory.

    The codex and aider finalizers create each case's judge output directory
    with ``mkdir(exist_ok=False)`` and judge every scoreable case unconditionally,
    so a second invocation on a finished run dies on the FIRST case that already
    has a directory.  Such a tree cannot be repaired by archiving one case and
    re-running; it needs a reuse guard in the tree itself.

    ``finalizer-reuse-guard`` marks a tree that has gained that guard
    (patch_15_codex_finalizer_reuse.py): it keeps the once-guard for a genuinely
    new case directory but reuses a bound verdict and routes a refused one to its
    own resample path, so the finalizer is re-runnable again.
    """
    text = finalizer_path(task_root).read_text(encoding="utf-8")
    if REUSE_GUARD_MARKER in text:
        return False
    return "mkdir(exist_ok=False)" in text


def finalizer_resamples_refused(task_root: Path) -> bool:
    """True when the tree itself retires a refused verdict and judges again.

    Such a tree repairs its own `model_output_invalid` case on a re-run, so an
    invalid bound contract is work the finalizer will do, not a blocker that
    needs the per-case repair tool.
    """
    text = finalizer_path(task_root).read_text(encoding="utf-8")
    return REUSE_GUARD_MARKER in text and "resample_refused_verdict" in text


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def broker_counter(stats, field: str) -> int:
    runtime = stats.get("runtime") if isinstance(stats, dict) else None
    value = (runtime or {}).get(field)
    return value if type(value) is int else -1


# ---------------------------------------------------------------------------
# run configuration
# ---------------------------------------------------------------------------
def find_launch_record(run_dir: Path, explicit=None) -> dict:
    if explicit is not None:
        return read_json(explicit)
    for candidate in sorted(LAUNCH_CONTROL.glob("*/*/launch_record.json")):
        try:
            value = read_json(candidate)
        except ValueError:
            continue
        if Path(str(value.get("run_dir", ""))).resolve() == run_dir:
            value["_record_path"] = str(candidate)
            return value
    raise Refusal(f"no launch_control launch_record.json points at {run_dir}; pass --launch-record")


def task_root_from_launch(launch: dict, explicit=None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    command = launch["command"]
    entry = next((token for token in command if token.endswith("formal_one_stop.py")), None)
    if entry is None:
        raise Refusal("launch record does not name the sibling one-stop entry point")
    return Path(entry).resolve().parents[1]


def credential_from_launch(launch: dict, explicit=None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    command = launch["command"]
    return Path(command[command.index("--credential-file") + 1]).resolve()


def broker_config(run_dir: Path, explicit=None) -> dict:
    """Recover the run's own Result-judge broker configuration.

    Every task writes judge_broker_runtime's transport directory somewhere in
    the run (run root for deeptutor, brokers/ for claude and openhands), so it
    is discovered rather than assumed.
    """
    if explicit is not None:
        launches = [Path(explicit).resolve()]
    else:
        launches = sorted(run_dir.glob("*-judge-transport/launch.json"))
        launches += sorted(run_dir.glob("*/*-judge-transport/launch.json"))
    if not launches:
        raise Refusal(f"no evaluator judge-broker launch evidence under {run_dir}")
    if len(launches) > 1:
        launches = [pick_result_judge_broker(launches)]
    command = read_json(launches[0])["command"]
    image = None
    for index, token in enumerate(command):
        if token == "python3" and index + 1 < len(command) and command[index + 1] == "/broker.py":
            image = command[index - 1]
            break
    if image is None:
        raise Refusal("could not recover the judge broker image from the run's launch evidence")
    upstream = command[command.index("--upstream") + 1]
    credential = None
    for index, token in enumerate(command):
        if token == "-v" and command[index + 1].endswith(":/run/secrets/agentswe.env:ro"):
            credential = command[index + 1].split(":", 1)[0]
    if credential is None:
        raise Refusal("could not recover the judge broker credential mount from the run's evidence")
    return {"image": image, "upstream": upstream, "credential": credential,
            "launch_evidence": str(launches[0])}


# ---------------------------------------------------------------------------
# finalizer family
# ---------------------------------------------------------------------------
def finalizer_path(task_root: Path) -> Path:
    path = task_root / "evaluator" / "formal_finalize.py"
    if not path.is_file():
        raise Refusal(f"missing tree finalizer: {path}")
    return path


def finalizer_family(task_root: Path) -> str:
    text = finalizer_path(task_root).read_text(encoding="utf-8")
    if "from formal_axes import main" in text:
        return "shared_main"
    if "from semantic_finalize import finalize" in text:
        return "semantic_finalize"
    return "standalone"


def finalizer_supports_output(task_root: Path, family: str) -> bool:
    if family in ("shared_main", "semantic_finalize"):
        return True  # both end in formal_axes_shared.main, which defines --output
    return '"--output"' in finalizer_path(task_root).read_text(encoding="utf-8")


def fail_fast_finalizer(task_root: Path, family: str) -> bool:
    """Standalone finalizers that abort the whole run on the first bad case.

    On those trees repairing one case necessarily judges every later case that
    never got a contract, so the caller must be told before it spends anything.
    """
    if family != "standalone":
        return False
    text = finalizer_path(task_root).read_text(encoding="utf-8")
    return "SemanticMeasurementUnavailable" in text or "return 2, result" in text


def endpoint_flag(family=None) -> str:
    """The endpoint flag this family's finalizer actually gates on.

    The semantic_finalize trees are invoked by their own one-stop with the
    legacy `--result-broker-endpoint`; their shim renames it to the shared name
    before delegating.  ai-scientist additionally REFUSES up front when that
    exact string is absent from argv, so the shared name is not interchangeable
    there.  Everyone else defines `--result-judge-broker-endpoint` directly.
    """
    return "--result-broker-endpoint" if family == "semantic_finalize" \
        else "--result-judge-broker-endpoint"


def finalizer_command(task_root: Path, run_dir: Path, credential: Path, endpoint: str,
                      output=None, family=None) -> list[str]:
    command = [str(CONTROL_PYTHON), "-E", "-s", "-B", str(finalizer_path(task_root)),
               "--run-dir", str(run_dir),
               "--credential-file", str(credential),
               endpoint_flag(family), endpoint]
    if output is not None:
        command.extend(["--output", str(output)])
    return command


def run_finalizer(command: list[str], label: str, log_dir: Path) -> dict:
    started = time.time()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    (log_dir / f"{label}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (log_dir / f"{label}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return {"label": label, "command": command, "returncode": completed.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "stdout_tail": completed.stdout[-2000:], "stderr_tail": completed.stderr[-2000:]}


# ---------------------------------------------------------------------------
# per-case judging evidence
# ---------------------------------------------------------------------------
def result_axis_root(run_dir: Path) -> Path:
    return run_dir / "formal_scoring" / "result_axis"


def contract_snapshot(run_dir: Path) -> dict:
    snapshot = {}
    root = result_axis_root(run_dir)
    if not root.is_dir():
        return snapshot
    # `test_*` also matches retired attempts (`test_006.attempt-001-…`), which are
    # evidence of a repair, not cases. Only exact case ids count.
    for case_dir in sorted(p for p in root.glob("test_*") if p.is_dir() and p.name in CASES):
        contract = case_dir / "result_score_contract.json"
        if contract.is_file():
            snapshot[case_dir.name] = sha256_file(contract)
    return snapshot


def identity_document(case_dir: Path) -> tuple:
    """Return (kind, declared inputs) for the case's immutable scoring inputs.

    shared_main / semantic_finalize bind the inputs in scoring_intent.json and
    hash the whole identity; standalone trees only leave the judge's own
    input_manifest.json, which records the same path+sha256 per input.
    """
    intent = case_dir / "scoring_intent.json"
    if intent.is_file():
        value = read_json(intent)
        identity = value["identity"]
        recomputed = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if recomputed != value.get("identity_sha256"):
            raise Refusal("scoring_intent.json identity hash does not reproduce from its own identity")
        return "scoring_intent", {
            "inputs": {name: {"path": entry["path"], "sha256": entry["sha256"]}
                       for name, entry in identity["inputs"].items()},
            "judge_source_digest": identity.get("judge_source_digest"),
            "model": identity.get("model"), "effort": identity.get("effort"),
            "identity_sha256": value["identity_sha256"]}
    manifest = case_dir / "input_manifest.json"
    if manifest.is_file():
        value = read_json(manifest)
        return "input_manifest", {
            "inputs": {name: {"path": entry["path"], "sha256": entry["sha256"]}
                       for name, entry in value.items()},
            "judge_source_digest": None, "model": None, "effort": None,
            "identity_sha256": None}
    raise Refusal(f"case dir carries neither scoring_intent.json nor input_manifest.json: {case_dir}")


def regenerated_inputs(case_dir: Path, declared: dict) -> list:
    """Inputs the finalizer rewrites inside the case dir before judging again.

    deeptutor regenerates oracle_comparison.json from the run-local private
    comparison; claude regenerates result_score_caps.json from the tree's
    score_caps module.  Both live inside the archived directory, so a repair
    judging only sees byte-identical inputs if the regeneration reproduces.
    """
    inside = []
    for name, entry in declared["inputs"].items():
        try:
            Path(entry["path"]).resolve().relative_to(case_dir.resolve())
        except ValueError:
            continue
        inside.append(name)
    return sorted(inside)


def repairs_on_disk(run_dir: Path) -> list:
    """Every completed per-case repair, recovered from the case directories.

    The tree finalizer rewrites formal_aggregation.json from scratch on every
    run, so repair accounting cannot live only in that file -- a later repair or
    re-finalization would silently drop an earlier one, and with it the record
    of the first attempt's provider usage.  Each repair also writes its block
    into the case's rejudge_record.json, which survives, so the list is rebuilt
    from there.
    """
    blocks = {}
    root = result_axis_root(run_dir)
    if not root.is_dir():
        return []
    for record_path in sorted(p for p in root.glob("test_*/rejudge_record.json")
                              if p.parent.name in CASES):
        try:
            value = read_json(record_path)
        except (OSError, ValueError):
            continue
        block = value.get("repair")
        if not isinstance(block, dict) or not block.get("case_id"):
            continue
        # `repair_succeeded` in older blocks also required the whole run to be
        # publishable at that moment, which says nothing about this case. The
        # case-level truth is re-derived from the contract that is on disk now.
        contract_path = record_path.with_name("result_score_contract.json")
        if contract_path.is_file():
            try:
                contract = read_json(contract_path)
            except (OSError, ValueError):
                contract = {}
            block = {**block,
                     "case_contract_valid_now": contract.get("contract_valid") is True,
                     "case_result_score_now": contract.get("result_score")}
        blocks[block["case_id"]] = block
    return [blocks[case] for case in sorted(blocks)]
