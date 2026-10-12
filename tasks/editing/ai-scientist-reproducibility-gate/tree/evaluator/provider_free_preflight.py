"""No-model, no-network mount/dependency/entrypoint check; never a score."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
import lower_agent_launcher as launcher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="agentswe/edit-candidate-python311:0826")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    case_file = ROOT / "agentloop/cases/test_001/case_input.json"
    workspace = output / "fresh_workspace"
    workspace.mkdir()
    excluded = launcher.copy_candidate_visible_assets(launcher.case_assets_root(case_file, case_file.parent), workspace)
    context = json.loads((workspace / "transaction_context.json").read_text())
    candidate = args.candidate.resolve()
    before = launcher.tree_digest(candidate)
    image = subprocess.run(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True, capture_output=True, check=True).stdout.strip()
    command = launcher.docker_product_command(candidate, output, context, "prepare", "http://unreachable.invalid/v1/responses", image, None, workspace=workspace)
    name = command[command.index("--name") + 1]
    result = {"schema_version": "agentswe-provider-free-preflight/v1", "case_id": "test_001",
              "candidate_digest": before, "image_id": image, "container_name": name,
              "model_requests_permitted": False, "network": "none", "score": None,
              "formal_result_publishable": False, "candidate_excluded_assets": excluded,
              "configuration_delta": "selective mounts, no host networking, no privileges; unchanged pinned image"}
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
        (output / "product_stdout.log").write_text(completed.stdout)
        (output / "product_stderr.log").write_text(completed.stderr)
        result["product_exit_code"] = completed.returncode
        result["source_unchanged"] = launcher.tree_digest(candidate) == before
        result["environment_entry_valid"] = completed.returncode == 0 and result["source_unchanged"]
        result["product_files"] = sorted(path.name for path in (output / "product_output").iterdir())
    except Exception as exc:
        result["environment_entry_valid"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = subprocess.run(["docker", "rm", "-f", name], text=True, capture_output=True, timeout=30, check=False)
        remaining = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"], text=True, capture_output=True, check=False)
        # The product runs with --rm: once it has exited, Docker 29 removes it asynchronously and `docker rm -f`
        # answers "removal of container ... is already in progress" while it is still listed. Wait for that.
        in_progress = cleanup.returncode != 0 and "already in progress" in cleanup.stderr.lower()
        wait_until = time.monotonic() + 30  # seconds; removal of a small container took ~7 s on an idle host
        while in_progress and remaining.stdout.strip() and time.monotonic() < wait_until:
            time.sleep(0.5)
            remaining = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"], text=True, capture_output=True, check=False)
        result["cleanup"] = {"container_name": name, "owned_runtime_only": True,
                             "remove_exit_code": cleanup.returncode, "absent": not remaining.stdout.strip()}
        if cleanup.returncode:
            result["cleanup"].update(remove_stderr=cleanup.stderr[-500:], removal_in_progress_at_rm=in_progress)
        launcher.write_json(output / "preflight.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["environment_entry_valid"] and result["cleanup"]["absent"] else 2


if __name__ == "__main__": raise SystemExit(main())
