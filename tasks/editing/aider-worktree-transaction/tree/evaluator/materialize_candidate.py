#!/usr/bin/env python3
"""Materialize and syntax-check a Candidate without mutating the source tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ALLOWED_PREFIXES = ("aider/", "tests/")
FORBIDDEN_PREFIXES = ("evaluator/", "dev_cases/", "test_cases/", "input/", "meta/", ".github/")
REQUIRED_DELIVERY = ("solution.patch", "edit_report.json", "run_report.json")
SYNTAX_IMAGE = "agentswe/edit-candidate-python311:0826"
MEMORY_BYTES = 4 * 1024 ** 3
TOTAL_SECONDS = 600
CLEANUP_SECONDS = 30
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
COMPILE_PROGRAM = r'''
import json, pathlib, sys
result = {"python_version": list(sys.version_info[:3]), "files_compiled": 0}
try:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("fixed lower image must provide Python 3.11")
    result["kernel_resources"] = {name: pathlib.Path("/sys/fs/cgroup", name).read_text().strip()
                                  for name in ("memory.max", "memory.swap.max", "cpu.max")}
    if (result["kernel_resources"]["memory.max"] != "4294967296"
            or result["kernel_resources"]["memory.swap.max"] != "0"
            or list(map(int, result["kernel_resources"]["cpu.max"].split())) != [400000, 100000]):
        raise RuntimeError("actual syntax container kernel resource limits differ")
    root = pathlib.Path("/candidate").resolve()
    paths = sorted(root.rglob("*.py"))
    if not paths:
        raise RuntimeError("no baseline Python sources")
    for path in paths:
        path.resolve().relative_to(root)
        # Compile source bytes without importing or executing Candidate code,
        # without writing pyc files, and with no Candidate sys.path entries.
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        result["files_compiled"] += 1
    result.update(valid=True, error_type=None)
except Exception as exc:
    result.update(valid=False, error_type=type(exc).__name__, error=str(exc)[-1200:])
print(json.dumps(result))
raise SystemExit(0 if result["valid"] else 1)
'''


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def resources_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evaluator.harness import owned_resources
    return owned_resources


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("shared materialization work deadline exhausted")
    return value


def command(argv, deadline, *, cwd="/", maximum=None):
    timeout = remaining(deadline)
    return subprocess.run(argv, cwd=cwd, env=CLEAN_ENV, text=True,
                          capture_output=True, check=False,
                          timeout=min(timeout, maximum) if maximum else timeout)


def cleanup_containers(evidence):
    """Remove only exact random owner/image/mount identities, including exits."""
    records = []
    for path in sorted(evidence.glob("container-*.json")):
        owned = json.loads(path.read_text())
        inspected = subprocess.run(["docker", "inspect", owned["name"]], env=CLEAN_ENV,
                                   capture_output=True, text=True, timeout=5)
        if inspected.returncode:
            if "no such object" not in inspected.stderr.lower() and "no such container" not in inspected.stderr.lower():
                raise RuntimeError("cannot prove syntax container absence")
            records.append({"name": owned["name"], "absent": True})
            continue
        actual = json.loads(inspected.stdout)[0]
        if (actual["Name"] != "/" + owned["name"] or actual["Image"] != owned["image_id"]
                or actual["Config"].get("Labels", {}).get("agentswe.aider.syntax.owner") != owned["token"]
                or not any(m["Source"] == owned["source"] and m["Destination"] == "/candidate"
                           for m in actual["Mounts"])):
            raise RuntimeError("syntax container owner mismatch; refusing cleanup")
        removed = subprocess.run(["docker", "rm", "-f", actual["Id"]], env=CLEAN_ENV,
                                 capture_output=True, text=True, timeout=8)
        after = subprocess.run(["docker", "inspect", actual["Id"]], env=CLEAN_ENV,
                               capture_output=True, text=True, timeout=5)
        absent = after.returncode != 0 and ("no such object" in after.stderr.lower() or "no such container" in after.stderr.lower())
        if removed.returncode or not absent:
            raise RuntimeError("owned syntax container cleanup incomplete")
        records.append({"name": owned["name"], "id": actual["Id"], "absent": True})
    return {"complete": True, "containers": records}


def syntax_check(source, evidence, phase, deadline):
    resources = resources_module()
    parent = resources._ambient_aggregate()
    if parent is None:
        raise RuntimeError("syntax check requires an actual Aider owned resource envelope")
    image = command(["docker", "image", "inspect", SYNTAX_IMAGE], deadline, maximum=10)
    if image.returncode:
        raise RuntimeError("fixed installed lower Python 3.11 image unavailable; no pull attempted")
    image_id = json.loads(image.stdout)[0]["Id"]
    token = uuid.uuid4().hex
    owned = {"name": "agentswe-aider-syntax-" + token, "token": token,
             "source": str((source / "aider").resolve()), "image_id": image_id}
    write_json(evidence / ("container-" + phase + ".json"), owned)
    argv = ["docker", "create", "--pull", "never", "--name", owned["name"], "--label", "agentswe.aider.syntax.owner=" + token,
            "--network", "none", "--read-only", "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--memory", str(MEMORY_BYTES), "--memory-swap", str(MEMORY_BYTES), "--cpus", "4",
            "--cgroup-parent", parent, "--mount", "type=bind,src=" + owned["source"] + ",dst=/candidate,readonly",
            "--workdir", "/", image_id, "python3", "-I", "-B", "-c", COMPILE_PROGRAM]
    created = command(argv, deadline, maximum=15)
    if created.returncode:
        raise RuntimeError("cannot create isolated syntax container: " + created.stderr[-500:])
    inspected = command(["docker", "inspect", owned["name"]], deadline, maximum=5)
    actual = json.loads(inspected.stdout)[0]
    host = actual["HostConfig"]
    if (actual["Image"] != image_id or host["NetworkMode"] != "none" or not host["ReadonlyRootfs"]
            or host["Memory"] != MEMORY_BYTES or host["MemorySwap"] != MEMORY_BYTES
            or host["NanoCpus"] != 4 * 10 ** 9 or host["CgroupParent"] != parent
            or "ALL" not in host.get("CapDrop", [])
            or not any(value.startswith("no-new-privileges") for value in host.get("SecurityOpt", []))
            or len(actual["Mounts"]) != 1
            or not any(m["Source"] == owned["source"] and m["Destination"] == "/candidate" and not m["RW"]
                       for m in actual["Mounts"])):
        raise RuntimeError("actual syntax container resource/isolation configuration differs")
    done = command(["docker", "start", "--attach", owned["name"]], deadline)
    stopped = command(["docker", "inspect", owned["name"]], deadline, maximum=5)
    state = json.loads(stopped.stdout)[0]["State"]
    try:
        observed = json.loads(done.stdout)
    except (ValueError, TypeError):
        observed = {"valid": False, "error_type": "MissingCompileEvidence"}
    outcome = {"command": "python3 -I -B -c <compile-only worker for aider/**/*.py>", "exit_code": done.returncode,
               "stderr_tail": (observed.get("error_type", "") or "") + ": " + observed.get("error", done.stderr[-1200:]),
               "runtime": "docker:" + SYNTAX_IMAGE, "image_id": image_id, "observed": observed,
               "container_state": {k: state.get(k) for k in ("ExitCode", "OOMKilled", "Error", "Status")},
               "resource_contract": {"memory_bytes": MEMORY_BYTES, "cpus": 4, "network": "none",
                                     "read_only": True, "aggregate_parent": parent},
               "valid": done.returncode == 0 and observed.get("valid") is True and not state.get("OOMKilled")
                        and state.get("ExitCode") == 0 and state.get("Status") == "exited" and not state.get("Error")}
    write_json(evidence / (phase + "-compile.json"), outcome)
    cleanup_containers(evidence)
    return outcome


def envelope_timeout():
    """Do not refresh an enclosing Aider work scope's absolute deadline."""
    resources = resources_module()
    if resources._ambient_aggregate() is None:
        return TOTAL_SECONDS
    current = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    unit = current.rsplit("/", 1)[-1]
    shown = subprocess.run(["systemctl", "show", unit, "--property=ActiveEnterTimestampMonotonic,RuntimeMaxUSec"],
                           env=CLEAN_ENV, capture_output=True, text=True, timeout=5, check=True)
    values = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    end = int(values["ActiveEnterTimestampMonotonic"]) / 1e6 + resources._duration_seconds(values["RuntimeMaxUSec"])
    return min(TOTAL_SECONDS, remaining(end))


def syntax_classification(outcome, baseline_image_id):
    if outcome["image_id"] != baseline_image_id or outcome["container_state"].get("OOMKilled"):
        return "evaluator_infrastructure_failure"
    if outcome["valid"]:
        return "ready_for_lower"
    if outcome["observed"].get("error_type") in {"SyntaxError", "IndentationError", "TabError"}:
        return "candidate_build_failure"
    return "evaluator_infrastructure_failure"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda x: x.relative_to(path).as_posix()):
        if ".git" in item.relative_to(path).parts or "__pycache__" in item.parts or item.suffix == ".pyc": continue
        rel = item.relative_to(path).as_posix().encode(); h.update(len(rel).to_bytes(8,"big")); h.update(rel)
        if item.is_symlink(): kind,payload=b"L",item.readlink().as_posix().encode()
        elif item.is_file(): kind,payload=b"F",item.read_bytes()
        else: kind,payload=b"D",b""
        h.update(kind); h.update(len(payload).to_bytes(8,"big")); h.update(payload)
    return h.hexdigest()


def validate_delivery(candidate: Path, changed_paths: list[str]) -> list[str]:
    errors: list[str] = []
    missing = [name for name in REQUIRED_DELIVERY if not (candidate / name).is_file()]
    if missing:
        errors.append(f"missing delivery files: {missing}")
        return errors
    bad_paths = [
        value for value in changed_paths
        if Path(value).is_absolute()
        or ".." in Path(value).parts
        or not value.startswith(ALLOWED_PREFIXES)
        or value.startswith(FORBIDDEN_PREFIXES)
    ]
    if bad_paths:
        errors.append(f"forbidden changed paths: {bad_paths}")
    if "aider/worktree_plan_adapter.py" not in changed_paths:
        errors.append("missing fixed entry aider/worktree_plan_adapter.py")
    if not any(value.startswith("tests/") and value.endswith(".py") for value in changed_paths):
        errors.append("patch must include focused source-adjacent Python tests")
    reports: dict[str, object] = {}
    for name in ("edit_report.json", "run_report.json"):
        try:
            value = json.loads((candidate / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid {name}: {type(exc).__name__}")
            continue
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            errors.append(f"invalid {name} schema")
            continue
        serialized = json.dumps(value, ensure_ascii=False)
        if any(token in serialized for token in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "SERPER_TOKEN", "fixture-private", "lease_token")):
            errors.append(f"sensitive content in {name}")
        reports[name] = value
    edit = reports.get("edit_report.json")
    if isinstance(edit, dict):
        if sorted(edit.get("changed_paths", [])) != sorted(changed_paths):
            errors.append("edit_report changed_paths mismatch")
        if not isinstance(edit.get("summary"), str) or not isinstance(edit.get("tests"), list):
            errors.append("edit_report summary/tests invalid")
    run = reports.get("run_report.json")
    if isinstance(run, dict):
        required = {"status", "commands", "duration_seconds", "errors", "deepseek", "gateway", "gateway_image", "serper", "web_retrieval"}
        if not required <= set(run):
            errors.append("run_report required fields missing")
        if any(run.get(key) != 0 for key in ("deepseek", "gateway", "gateway_image", "serper", "web_retrieval")):
            errors.append("run_report provider counts must be zero")
    return errors


def materialize(a, evidence, deadline) -> int:
    if a.output.exists():
        raise RuntimeError("materialization output must be new; preserve previous attempt evidence")
    shutil.copytree(a.source,a.output,symlinks=True)
    result={"schema_version":"agentswe-candidate-build-v1","source":str(a.source),"output":str(a.output),"candidate_digest":None,"changed_paths":[]}
    try:
        baseline = syntax_check(a.output, evidence, "baseline", deadline)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result.update(classification="evaluator_infrastructure_failure", environment_preflight={"valid": False}, error=str(exc))
        (a.output / "build_result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 3
    result["environment_preflight"] = {"valid": baseline["valid"], "evaluator_uid": os.geteuid(),
                                       "baseline_compile_exit": baseline["exit_code"], "python": baseline["runtime"],
                                       "syntax_evidence": str(evidence), "baseline_compile": baseline}
    if not baseline["valid"]:
        result.update({"classification": "evaluator_infrastructure_failure", "error": "unchanged baseline compile failed",
                       "stderr_tail": baseline["stderr_tail"][-1500:]})
        (a.output / "build_result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 3
    patch=a.patch.read_text(encoding="utf-8")
    result["changed_paths"]=[line[6:].split("\t",1)[0] for line in patch.splitlines() if line.startswith("+++ b/")]
    delivery_errors = validate_delivery(a.candidate, result["changed_paths"])
    if not result["changed_paths"] or any(Path(x).is_absolute() or ".." in Path(x).parts for x in result["changed_paths"]): result["classification"]="candidate_build_failure"; result["error"]="unsafe_or_empty_patch"
    elif delivery_errors:
        result["classification"]="candidate_build_failure"; result["error"]="delivery_contract"; result["delivery_errors"]=delivery_errors
    else:
        checks=[]
        for cmd in (["git","apply","--check",str(a.patch)], ["git","apply",str(a.patch)]):
            done=command(cmd,deadline,cwd=a.output); checks.append({"command":cmd,"exit_code":done.returncode,"stderr_tail":done.stderr[-1200:]})
            if done.returncode:
                result["classification"] = "evaluator_infrastructure_failure" if any(token in done.stderr.lower() for token in ("permission denied", "no space left", "read-only file system")) else "candidate_build_failure"
                result["error"]="patch_apply"
                break
        if not result.get("classification"):
            done=syntax_check(a.output,evidence,"candidate",deadline)
            checks.append(done)
            # Only a structured compiler syntax error after a healthy baseline
            # is Candidate failure. OOM, resource/launch/read errors are infra.
            result["classification"] = syntax_classification(done, baseline["image_id"])
            if done["image_id"] != baseline["image_id"]:
                result.update(classification="evaluator_infrastructure_failure", error="syntax runtime changed after baseline")
        result["checks"]=checks
    result["candidate_digest"]=digest(a.output); a.output.joinpath("build_result.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8"); print(json.dumps(result,indent=2)); return 0 if result["classification"]=="ready_for_lower" else 2


def main() -> int:
    p=argparse.ArgumentParser()
    for name in ("source", "candidate", "output", "patch"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--syntax-worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--syntax-evidence", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--syntax-deadline", type=float, help=argparse.SUPPRESS)
    a=p.parse_args()
    for name in ("source", "candidate", "output", "patch"):
        setattr(a, name, getattr(a, name).resolve())
    if a.syntax_worker:
        return materialize(a, a.syntax_evidence, a.syntax_deadline)
    if a.output.exists():
        raise RuntimeError("materialization output must be new; preserve previous attempt evidence")
    evidence = a.output.parent / ("." + a.output.name + ".syntax-" + uuid.uuid4().hex)
    evidence.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": "agentswe-candidate-build-v1", "source": str(a.source),
              "output": str(a.output), "candidate_digest": None, "changed_paths": [],
              "environment_preflight": {"valid": False}, "syntax_evidence": str(evidence)}
    error = None
    attestation = None
    try:
        timeout = envelope_timeout()
        if timeout <= CLEANUP_SECONDS:
            raise TimeoutError("insufficient existing materialization budget for verified cleanup")
        deadline = time.monotonic() + timeout - CLEANUP_SECONDS
        argv = [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                "--syntax-worker", "--syntax-evidence", str(evidence), "--syntax-deadline", str(deadline)]
        for name in ("source", "candidate", "output", "patch"):
            argv.extend(["--" + name, str(getattr(a, name))])
        done, attestation = resources_module().run_owned(argv, cwd="/", env=CLEAN_ENV,
                        output=evidence / "envelope", timeout=timeout, purpose="case")
        if (a.output / "build_result.json").is_file():
            result = json.loads((a.output / "build_result.json").read_text())
        if attestation.get("timed_out") or not attestation.get("valid") or done.returncode not in (0, 2, 3):
            raise RuntimeError("isolated syntax/materialization process did not complete: exit " + str(done.returncode))
        if not result.get("classification"):
            raise RuntimeError("materialization result missing")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            cleanup = cleanup_containers(evidence)
        except Exception as exc:
            cleanup = {"complete": False, "error": type(exc).__name__ + ": " + str(exc)}
            error = "owned syntax container cleanup failed"
        write_json(evidence / "cleanup.json", cleanup)
    if error:
        result.update(classification="evaluator_infrastructure_failure", error=error)
    result["syntax_evidence"] = str(evidence)
    result["syntax_cleanup"] = cleanup
    result["syntax_resource_attestation"] = str(evidence / "envelope/resource-attestation.json")
    a.output.mkdir(parents=True, exist_ok=True)
    write_json(a.output / "build_result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["classification"] == "ready_for_lower" else (3 if result["classification"] == "evaluator_infrastructure_failure" else 2)


if __name__=="__main__": raise SystemExit(main())
