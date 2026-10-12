#!/usr/bin/env python3
"""Run the authoritative Create Code judge with an evaluator-only secret mount.

This wrapper intentionally preserves code_eval.py unchanged.  It maps the
frozen Candidate, public requirements, task-local rubric, and output directory
into a short-lived evaluator container.  The real credential file is mounted
read-only at /run/secrets and is never copied to Candidate-visible storage.
"""

from __future__ import annotations

import argparse
import re
import hashlib
import json
import signal
import subprocess
import uuid
from pathlib import Path


CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
USAGE_CAPTURE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_sitecustomize.py")
# The paper pinned this image by id (sha256:b3fd1b7e9e1a). A rebuilt image gets
# a new id, so the release pins its content instead: the image must carry the
# digest of the shipped build context (images/edit-code-judge, every input
# digest-pinned), which agentswe setup writes as a label when it builds it.
DEFAULT_IMAGE = "@@AGENTSWE_CODE_JUDGE_IMAGE@@"
CONTEXT_SHA256 = "@@AGENTSWE_CODE_JUDGE_CONTEXT_SHA256@@"
EDIT_ENTRY = Path(__file__).with_name("code_judge_entry.py")
TRANSPORT = Path(__file__).with_name("result_judge.py")
STREAM_MODULE = Path(__file__).with_name('responses_stream.py')


def cleanup_owned_container(target: str, ownership: str) -> dict:
    """Remove only this invocation's labelled container; absence must be observed."""
    receipt = {"target": target, "ownership": ownership, "complete": False}
    try:
        observed = subprocess.run(["docker", "container", "inspect", target],
                                  text=True, capture_output=True, timeout=20, check=False)
        if observed.returncode:
            absent = any(marker in observed.stderr.lower()
                         for marker in ("no such object", "no such container"))
            return {**receipt, "complete": absent, "already_absent": absent,
                    "inspect_exit_code": observed.returncode}
        rows = json.loads(observed.stdout)
        if (not isinstance(rows, list) or len(rows) != 1
                or (rows[0].get("Config", {}).get("Labels") or {}).get("agentswe.code-judge.owner") != ownership):
            return {**receipt, "error": "container ownership mismatch; no cleanup attempted"}
        identity = rows[0]["Id"]
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            return {**receipt, "error": "invalid inspected container identity"}
        removed = subprocess.run(["docker", "container", "rm", "-f", identity],
                                 text=True, capture_output=True, timeout=25, check=False)
        check = subprocess.run(["docker", "container", "inspect", identity],
                               text=True, capture_output=True, timeout=20, check=False)
        absent = check.returncode != 0 and any(marker in check.stderr.lower()
                    for marker in ("no such object", "no such container"))
        return {**receipt, "container_id": identity, "remove_exit_code": removed.returncode,
                "complete": absent, "verified_absent": absent}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {**receipt, "error": type(exc).__name__}


def assert_image_content(image: str) -> None:
    """Refuse an image that was not built from the shipped Code judge context."""
    observed = subprocess.run(["docker", "image", "inspect", image, "--format",
                               '{{index .Config.Labels "io.agentswe.context-sha256"}}'],
                              text=True, capture_output=True, timeout=20, check=False)
    if observed.returncode or observed.stdout.strip() != CONTEXT_SHA256:
        raise SystemExit("Code judge image %s was not built from the shipped context (context digest %s expected)"
                         % (image, CONTEXT_SHA256[:16]))


class RunnerInterrupted(Exception):
    def __init__(self, number):
        self.number = number


def record_usage_capture(output: Path) -> None:
    """Keep auxiliary observations separate from the bound transport ledger."""
    contract_path = output / 'code_score_contract.json'
    capture_path = output / 'provider_usage_capture.json'
    if not contract_path.is_file() or not capture_path.is_file():
        return
    try:
        contract = json.loads(contract_path.read_text(encoding='utf-8'))
        raw_capture = capture_path.read_bytes()
        capture = json.loads(raw_capture)
        if not isinstance(contract, dict) or not isinstance(capture, dict):
            return
        contract["provider_usage_capture"] = {
            'path': str(capture_path),
            'sha256': hashlib.sha256(raw_capture).hexdigest(),
            'observations': capture.get('observations', 0),
            'authoritative': False,
            'source': 'auxiliary HTTP observation; Code transport ledger remains authoritative',
        }
        contract_path.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    except (OSError, json.JSONDecodeError):
        return


def run_owned_container(command: list[str], output: Path, phase: str, timeout: int):
    """Bound docker-client lifetime and always audit its unique container cleanup."""
    ownership = uuid.uuid4().hex
    command = list(command)
    name_index = command.index("--name") + 1
    owned_name = command[name_index] + "-" + ownership[:8]
    command[name_index] = owned_name
    command[name_index + 1:name_index + 1] = ["--label", "agentswe.code-judge.owner=" + ownership]
    receipt = {"schema_version": "agentswe-code-container-lifecycle/v1", "phase": phase,
               "container_name": owned_name, "ownership": ownership, "wall_timeout_seconds": timeout,
               "credential_values_recorded": False}
    path = output / ("code_" + phase + "_lifecycle.json")
    # A crashed or concurrent prior invocation must not lose its ownership evidence.
    with path.open("x", encoding="utf-8") as handle:
        json.dump({**receipt, "state": "starting"}, handle)
    handlers = {}
    def interrupted(number, frame):
        raise RunnerInterrupted(number)
    stdout, stderr, code = "", "", 1
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, interrupted)
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
        stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
        receipt["state"] = "exited"
    except subprocess.TimeoutExpired as exc:
        def as_text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        stdout, stderr, code = as_text(exc.stdout), as_text(exc.stderr), 124
        receipt["state"] = "outer_timeout_infrastructure_invalid"
    except RunnerInterrupted as exc:
        code = 128 + exc.number
        receipt.update(state="interrupted_infrastructure_invalid", signal=exc.number)
    except OSError as exc:
        code, stderr = 127, type(exc).__name__
        receipt["state"] = "docker_client_unavailable"
    finally:
        # Restore after cleanup so the first normal termination gets a receipt.
        try:
            receipt["cleanup"] = cleanup_owned_container(owned_name, ownership)
        finally:
            for number, previous in handlers.items():
                signal.signal(number, previous)
        receipt["exit_code"] = code
        path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if not receipt["cleanup"]["complete"]:
        code = code or 2
        stderr += "\nCode container cleanup could not be verified; evaluator stage is incomplete.\n"
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def changed_paths_from_delivery(candidate: Path) -> tuple[list[str], str]:
    """Candidate-relative paths the accepted solution.patch touches.

    The Code axis measures the Candidate's change, so the evidence pack should
    carry that change in full rather than the whole upstream tree. Returns an
    empty focus and a reason when the delivery cannot be located, which leaves
    the pack exactly as it was.
    """
    # The frozen candidate is not always two levels below the run: openwiki
    # records .../lifecycle/frozen_candidate/repository, one deeper, and the
    # fixed expression probed .../lifecycle/lifecycle/ and missed, so every one
    # of its runs packed the whole repository. Bounded so the walk cannot leave
    # the run directory; layouts that already worked find it on the same step.
    manifest = None
    probe = candidate
    for _ in range(5):
        probe = probe.parent
        if probe == probe.parent:
            break
        found = probe / 'lifecycle/freeze_manifest.json'
        if found.is_file():
            manifest = found
            break
    if manifest is None:
        return [], 'no freeze manifest above the frozen candidate'
    try:
        accepted = json.loads(manifest.read_bytes()).get('accepted_candidate_path')
    except (OSError, ValueError) as exc:
        return [], 'freeze manifest unreadable: ' + type(exc).__name__
    if not accepted:
        return [], 'freeze manifest records no accepted_candidate_path'
    # accepted_candidate_path names the repository copy; the delivery that
    # produced it is its sibling.
    found = sorted(Path(accepted).parent.rglob('solution.patch'))
    if len(found) != 1:
        return [], 'expected one solution.patch beside the accepted candidate, found %d' % len(found)
    try:
        text = found[0].read_text(errors='replace')
    except OSError as exc:
        return [], 'solution.patch unreadable: ' + type(exc).__name__
    paths: set[str] = set()
    for match in re.finditer(r'^\+\+\+ b/(\S+)', text, re.M):
        paths.add(match.group(1))
    for match in re.finditer(r'^diff --git a/(\S+) b/(\S+)', text, re.M):
        paths.add(match.group(2))
    paths.discard('/dev/null')
    present = sorted(path for path in paths if (candidate / path).is_file())
    if not present:
        return [], 'solution.patch touches nothing present in the frozen candidate'
    return present, 'derived %d path(s) from %s' % (len(present), found[0].name)


LOCKFILE_NAMES = frozenset({
    'Cargo.lock', 'MODULE.bazel.lock', 'package-lock.json', 'pnpm-lock.yaml',
    'yarn.lock', 'npm-shrinkwrap.json', 'poetry.lock', 'uv.lock', 'Pipfile.lock',
    'composer.lock', 'Gemfile.lock', 'go.sum', 'flake.lock', 'bun.lockb',
    'packages.lock.json', 'gradle.lockfile',
})


def omit_unreviewable_evidence(paths: list[str], candidate: Path) -> tuple[list[str], list[dict]]:
    """Drop machine-generated lockfiles from the packed evidence, and say so.

    codex's Code judge was refused outright for exceeding the model's context by
    4.3%, while one Cargo.lock accounted for 11.8% of the evidence. A lockfile
    tells a Code judge nothing its own changed-path list does not already say.

    Only exact filenames are matched: no size heuristic, and no truncation of
    source, because a half-read source file damages a review invisibly while a
    missing lockfile does not. If every changed path is a lockfile the original
    list is returned untouched -- an empty focus set means `packs_whole_tree`,
    which would pack the entire repository instead of less of it.
    """
    kept: list[str] = []
    omitted: list[dict] = []
    for relative in paths:
        if Path(relative).name in LOCKFILE_NAMES:
            source = candidate / relative
            omitted.append({
                'path': relative, 'reason': 'machine-generated lockfile',
                'bytes': source.stat().st_size if source.is_file() else None,
            })
        else:
            kept.append(relative)
    if not kept:
        return paths, []
    return kept, omitted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--public-requirements", type=Path, required=True)
    parser.add_argument("--code-rubric", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-candidate-digest", default="")
    parser.add_argument("--evidence-path", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--transport-mode", choices=("stream", "nonstream"), default="stream")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    assert_image_content(args.image)
    candidate = args.candidate_source.resolve()
    requirements = args.public_requirements.resolve()
    rubric = args.code_rubric.resolve()
    credential = args.credential_file.resolve()
    output = args.output_dir.resolve()
    evidence_focus = list(args.evidence_path)
    focus_reason = 'explicit --evidence-path' if evidence_focus else ''
    if not evidence_focus:
        evidence_focus, focus_reason = changed_paths_from_delivery(candidate)
    evidence_focus, omitted_evidence = omit_unreviewable_evidence(evidence_focus, candidate)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'code_evidence_focus.json').write_text(json.dumps({
        'schema_version': 'agentswe-code-evidence-focus/v1',
        'paths': evidence_focus, 'reason': focus_reason,
        'packs_whole_tree': not evidence_focus,
        'omitted_paths': omitted_evidence,
    }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    missing = [str(path) for path in (candidate, requirements, rubric, credential, CREATE_CODE_JUDGE, EDIT_ENTRY, TRANSPORT, STREAM_MODULE, USAGE_CAPTURE) if not path.exists()]
    if missing:
        raise SystemExit("missing Code judge input: " + ", ".join(missing))
    if not candidate.is_dir() or not requirements.is_dir() or not rubric.is_file() or not credential.is_file():
        raise SystemExit("Code judge input types are invalid")
    if credential.stat().st_mode & 0o077:
        raise SystemExit("credential file must not be group/world accessible")
    output.mkdir(parents=True, exist_ok=True)
    usage_capture = output / "provider_usage_capture.json"
    if not args.preflight_only and ((output / "code_logical_request_started.json").exists()
                                    or (output / "code_score_contract.json").exists()):
        raise SystemExit("existing Code request evidence: refusing overwrite/resampling")
    owned_name = "agentswe-code-judge-" + hashlib.sha256(str(output).encode()).hexdigest()[:20]
    command = [
        "docker", "run", "--rm", "--network", "host",
        "--name", owned_name,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-v", f"{CREATE_CODE_JUDGE}:/judge/code_eval.py:ro",
        "-v", f"{EDIT_ENTRY}:/judge/code_judge_entry.py:ro",
        "-v", f"{TRANSPORT}:/judge/result_judge.py:ro",
        '-v', f'{STREAM_MODULE}:/judge/responses_stream.py:ro',
        "-v", f"{candidate}:/inputs/candidate:ro",
        "-v", f"{requirements}:/inputs/requirements:ro",
        "-v", f"{rubric}:/inputs/code_rubric.md:ro",
        "-v", f"{credential}:/run/secrets/agentswe.env:ro",
        "-v", f"{output}:/output",
        "-v", f"{USAGE_CAPTURE}:/opt/agentswe_code_judge/sitecustomize.py:ro",
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "-e", "AGENTSWE_EVALUATOR_PROXY_URL=",  # direct egress; mihomo lost whole responses under load
        "-e", "PYTHONPATH=/opt/agentswe_code_judge:/opt/agentswe-code-deps",
        "-e", "CODE_JUDGE_USAGE_CAPTURE=/output/provider_usage_capture.json",
        args.image,
        "python3", "/judge/code_judge_entry.py",
        "--candidate-source", "/inputs/candidate",
        "--public-requirements", "/inputs/requirements",
        "--code-rubric", "/inputs/code_rubric.md",
        "--credential-file", "/run/secrets/agentswe.env",
        "--output-dir", "/output",
        "--timeout", str(args.timeout),
        "--transport-mode", args.transport_mode,
    ]
    if args.expected_candidate_digest:
        command.extend(["--expected-candidate-digest", args.expected_candidate_digest])
    for evidence_path in evidence_focus:
        # Evidence paths are Candidate-relative in the formal protocol.
        normalized = Path(evidence_path).as_posix().lstrip("/")
        command.extend(["--evidence-path", normalized])
    preflight = list(command)
    preflight[preflight.index("--network") + 1] = "none"
    secret_index = preflight.index(f"{credential}:/run/secrets/agentswe.env:ro")
    del preflight[secret_index - 1:secret_index + 1]
    image_index = preflight.index(args.image)
    preflight[image_index:image_index] = ["-e", "AGENTSWE_CODE_JUDGE_PREFLIGHT=1"]
    checked = run_owned_container(preflight, output, "preflight", 180)
    (output / "code_preflight.stdout.log").write_text(checked.stdout)
    (output / "code_preflight.stderr.log").write_text(checked.stderr)
    if checked.returncode or args.preflight_only:
        print(json.dumps({"preflight_valid": checked.returncode == 0,
                          "provider_calls": 0, "output": str(output)}))
        return checked.returncode
    # All transient attempts share the existing transport deadline. The
    # overhead allowance is only for packing/container teardown, not the model.
    completed = run_owned_container(command, output, "judge", args.timeout + 180)
    (output / "code_judge.stdout.log").write_text(completed.stdout)
    (output / "code_judge.stderr.log").write_text(completed.stderr)
    record_usage_capture(output)
    invocation = {
        "schema_version": "agentswe-edit-code-judge-invocation-v1",
        "authoritative_judge": str(CREATE_CODE_JUDGE),
        "authoritative_judge_sha256": hashlib.sha256(CREATE_CODE_JUDGE.read_bytes()).hexdigest(),
        "edit_transport_entry_sha256": hashlib.sha256(EDIT_ENTRY.read_bytes()).hexdigest(),
        "transport_sha256": hashlib.sha256(TRANSPORT.read_bytes()).hexdigest(),
        'stream_module_sha256': hashlib.sha256(STREAM_MODULE.read_bytes()).hexdigest(),
        "container_name": completed.args[completed.args.index("--name") + 1],
        "container_image": args.image,
        "container_image_context_sha256": CONTEXT_SHA256,
        "credential_boundary": "evaluator-only read-only Docker secret mount",
        "candidate_mount": "read-only",
        "public_requirements_mount": "read-only",
        "rubric_mount": "read-only",
        "output_mount": "evaluator-owned writable",
        "exit_code": completed.returncode,
        "credential_values_recorded": False,
        "usage_capture": str(usage_capture),
    }
    (output / "code_judge_invocation.json").write_text(
        json.dumps(invocation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
