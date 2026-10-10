#!/usr/bin/env python3
"""Remove only evaluator-owned Harbor Compose residuals for one completed run."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path("@@AGENTSWE_EDITING_RUNS@@/smoke").resolve()


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def inspect_json(kind: str, identifier: str) -> dict[str, Any] | None:
    result = command(["docker", kind, "inspect", identifier])
    if result.returncode != 0:
        # Two phrasings for the same fact: "No such container: <id>" and
        # "network <id> not found". Only the first was recognised, so a network
        # that vanished between listing and inspection was raised as an
        # infrastructure failure instead of being reported absent.
        if re.search(r'no such (?:object|container|network)\b'
                     r'|\b(?:network|volume|image)\s+\S+\s+not found\b', result.stderr, re.I):
            return None
        raise RuntimeError('docker inspect failed without absence evidence: ' + result.stderr[-500:])
    try:
        value = json.loads(result.stdout)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0]
        raise ValueError('invalid Docker inspect shape')
    except (ValueError, TypeError, IndexError):
        raise RuntimeError('Docker inspect returned malformed evidence')


def terminal_trial_result(run: Path) -> Path:
    """The one Builder trial result.json that ends this run.

    A run whose Builder was never cut has exactly one trial and that trial is
    terminal. When a provider outage cut the session and the segment loop
    resumed it, Harbor opens a second trial under the same job, so the run
    legitimately carries one result.json per segment and the terminal record is
    the last segment's. Nothing is relaxed: the run must still have exactly one
    Harbor job; when a segment ledger exists it is the authority and the trial
    directories carrying a result.json must be exactly the segments it names,
    so a run whose LAST segment left no record is still refused; and a run with
    no ledger must still hold exactly one trial.
    """
    jobs = sorted((run / 'jobs').glob('*/result.json'))
    trials = sorted((run / 'jobs').glob('*/*/result.json'))
    if len(jobs) != 1 or not trials:
        raise RuntimeError('run has no Harbor terminal evidence of its own')
    job_dir = jobs[0].parent
    if any(path.parent.parent != job_dir for path in trials):
        raise RuntimeError('run has no Harbor terminal evidence of its own')
    ledger_path = run / 'builder_segments.json'
    if ledger_path.is_symlink():
        raise RuntimeError('run has no Harbor terminal evidence of its own')
    if not ledger_path.is_file():
        # No segment loop ever wrote here: the original one-trial rule stands.
        if len(trials) != 1:
            raise RuntimeError('run has no Harbor terminal evidence of its own')
        return trials[0]
    try:
        ledger = json.loads(ledger_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeError('builder segment ledger unreadable: ' + type(exc).__name__) from exc
    segments = ledger.get('segments')
    if (ledger.get('schema_version') != 'agentswe-builder-segments/v1'
            or not isinstance(segments, list) or not segments
            or [segment.get('segment_index') for segment in segments] != list(range(1, len(segments) + 1))):
        raise RuntimeError('builder segment ledger does not describe this run')
    named = [segment.get('trial') for segment in segments]
    if (any(not isinstance(name, str) or not name or '/' in name for name in named)
            or len(set(named)) != len(named)
            or sorted(named) != sorted(path.parent.name for path in trials)):
        raise RuntimeError('run has no Harbor terminal evidence of its own')
    terminal = job_dir / named[-1] / 'result.json'
    if terminal.is_symlink() or not terminal.is_file():
        raise RuntimeError('run has no Harbor terminal evidence of its own')
    return terminal


def _receipt_terminal_unit(unit: str, run: Path, observed: dict[str, str]) -> dict[str, str]:
    """Attest a unit whose exit systemd no longer remembers.

    Reached only after the live check above has refused: systemd releases a
    successful unit and blanks its exit fields, so without this a run that
    succeeded can never be finalized while one that failed can.
    """
    receipt_path = run / 'unit_exit_receipt.json'
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RuntimeError('same run unit has no authoritative terminal evidence')
    try:
        receipt = json.loads(receipt_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeError('unit exit receipt unreadable: ' + type(exc).__name__) from exc
    if (receipt.get('owner') != 'evaluator' or receipt.get('unit') != unit
            or receipt.get('run_id') != run.name):
        raise RuntimeError('unit exit receipt is not this unit and this run')
    invocation = receipt.get('invocation_id')
    if not isinstance(invocation, str) or not re.fullmatch('[0-9a-f]{32}', invocation):
        raise RuntimeError('unit exit receipt carries no invocation identity')
    if receipt.get('service_result') in (None, ''):
        raise RuntimeError('unit exit receipt carries no attested outcome')
    # The run's own end, proven without systemd. harbor_terminal() asks the same
    # of the job and trial results; this only requires that they exist at all,
    # so the caller's own check stays the authority on their contents.
    terminal_trial_result(run)
    snapshot = receipt.get('unit_properties_at_exit')
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    value = {key: snapshot.get(key, observed.get(key, '')) for key in
             ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainPID',
              'ExecMainExitTimestamp', 'ExecStart')}
    value['InvocationID'] = invocation
    value['terminal_evidence_source'] = 'unit_exit_receipt'
    if not re.search(re.escape(str(run)) + r'(?=[\s;\"\'}]|$)', value.get('ExecStart', '')):
        raise RuntimeError('terminal unit is not bound to the requested run')
    return value


def require_terminal_unit(unit: str, run: Path) -> dict[str, str]:
    result = command(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState',
        '-p', 'SubState', '-p', 'MainPID', '-p', 'ExecMainPID', '-p', 'ExecMainExitTimestamp',
        '-p', 'ExecStart', '-p', 'InvocationID'])
    if result.returncode:
        raise RuntimeError('unit observation failed; cleanup refused')
    value = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    # Never attest a unit that is still doing something, whatever any receipt
    # says. This is the property the whole check exists for and it is unchanged.
    if value.get('ActiveState') not in {'inactive', 'failed', ''} or value.get('MainPID') not in {'0', ''}:
        raise RuntimeError('same run unit has no authoritative terminal evidence')
    if (value.get('LoadState') != 'loaded' or value.get('ActiveState') not in {'inactive', 'failed'} or
            value.get('MainPID') != '0' or not value.get('ExecMainPID', '0').isdigit() or
            int(value.get('ExecMainPID', '0')) <= 0 or value.get('ExecMainExitTimestamp') in {None, '', 'n/a'}):
        return _receipt_terminal_unit(unit, run, value)
    value.setdefault('terminal_evidence_source', 'systemd')
    if not re.search(re.escape(str(run)) + r'(?=[\s;\"\'}]|$)', value.get('ExecStart', '')):
        raise RuntimeError('terminal unit is not bound to the requested run')
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument('--unit', required=True, help='exact systemd unit that owned this completed run')
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if ROOT not in run.parents or not run.is_dir():
        raise SystemExit(f"run directory must be an existing child of {ROOT}: {run}")
    working_dir = str(run / "builder_task" / "environment")
    terminal = require_terminal_unit(args.unit, run)
    result: dict[str, Any] = {
        "schema_version": "agentswe-edit-compose-residual-cleanup-v1",
        "run_dir": str(run),
        "ownership_selector": {"com.docker.compose.project.working_dir": working_dir},
        "containers": [],
        "networks": [],
        "unrelated_containers_touched": False,
        "completed": False,
        "terminal_unit": terminal,
    }

    listed = command(["docker", "ps", "-a", "--format", "{{.ID}}"])
    if listed.returncode != 0:
        result["error"] = "docker ps failed"
        write_json(run / "posthoc_compose_cleanup_attestation.json", result)
        return 2
    owned_ids: list[str] = []
    known_projects: set[str] = set()
    for identifier in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
        detail = inspect_json("container", identifier)
        if not detail:
            continue
        labels = detail.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project.working_dir") != working_dir:
            continue
        project = labels.get("com.docker.compose.project")
        if isinstance(project, str) and project:
            known_projects.add(project)
        owned_ids.append(identifier)
        name = str(detail.get("Name", "")).lstrip("/")
        # Capture only non-secret resource evidence, never Config.Env.
        stats = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', identifier])
        if stats.returncode:
            raise RuntimeError('owned container statistics could not be saved')
        evidence = {'container_id': identifier, 'name': name, 'state': detail.get('State'),
                    'labels': labels, 'stats_stdout': stats.stdout,
                    'mounts': [{k: m.get(k) for k in ('Type', 'Source', 'Destination', 'RW')}
                               for m in detail.get('Mounts', [])]}
        stats_path = run / 'compose_cleanup_stats' / (identifier + '.json')
        write_json(stats_path, evidence)
        require_terminal_unit(args.unit, run)
        removal = command(["docker", "rm", "-f", identifier])
        after = inspect_json('container', identifier)
        result["containers"].append({
            "container_id": identifier,
            "name": name,
            "working_dir": working_dir,
            "ownership_proven": True,
            "remove_exit_code": removal.returncode,
            "stats_path": str(stats_path),
            "absent_after_cleanup": after is None,
            "remove_stderr_tail": removal.stderr[-500:],
        })

    networks = command(["docker", "network", "ls", "--format", "{{.Name}}"])
    if networks.returncode != 0:
        result["error"] = "docker network ls failed"
        write_json(run / "posthoc_compose_cleanup_attestation.json", result)
        return 2
    for network in [line.strip() for line in networks.stdout.splitlines() if line.strip()]:
        detail = inspect_json("network", network)
        if not detail:
            continue
        labels = detail.get("Labels", {})
        project = str(labels.get("com.docker.compose.project", "")) if isinstance(labels, dict) else ""
        if not isinstance(labels, dict) or not project.startswith("builder_task__") or project not in known_projects:
            continue
        members = detail.get("Containers", {})
        if not isinstance(members, dict):
            continue
        member_records: list[dict[str, Any]] = []
        safe = True
        for member_id, member in members.items():
            member_detail = inspect_json("container", member_id)
            member_labels = member_detail.get("Config", {}).get("Labels", {}) if member_detail else {}
            member_working_dir = member_labels.get("com.docker.compose.project.working_dir") if isinstance(member_labels, dict) else None
            member_records.append({"container_id": member_id, "name": member.get("Name"), "working_dir": member_working_dir})
            if member_working_dir != working_dir:
                safe = False
        if members and not safe:
            continue
        require_terminal_unit(args.unit, run)
        removal = command(["docker", "network", "rm", network])
        after = inspect_json("network", network)
        result["networks"].append({
            "name": network,
            "project": labels.get("com.docker.compose.project"),
            "members": member_records,
            "ownership_proven": True,
            "remove_exit_code": removal.returncode,
            "absent_after_cleanup": after is None,
            "remove_stderr_tail": removal.stderr[-500:],
        })

    result["completed"] = all(item.get("absent_after_cleanup") is True for item in result["containers"]) and all(item.get("absent_after_cleanup") is True for item in result["networks"])
    result["selected_container_count"] = len(owned_ids)
    write_json(run / "posthoc_compose_cleanup_attestation.json", result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
