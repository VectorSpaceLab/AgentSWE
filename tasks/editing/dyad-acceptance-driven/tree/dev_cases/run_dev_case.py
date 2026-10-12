#!/usr/bin/env python3
"""Apply once and run one public Dyad Acceptance development scenario."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from public_harness import (
    PublicRunError,
    command,
    copy_apply,
    dependencies_ready,
    install_env,
    load_json,
    offline_env,
    score_public,
    validate_submission,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("dev_001", "dev_002"))
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--submission", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--skip-install", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent
    result: dict[str, object] = {
        "case_id": args.case,
        "valid": False,
        "commands": [],
        "errors": [],
    }
    try:
        repository = args.repository.resolve()
        submission = args.submission.resolve()
        worktree = args.work_root.resolve() / f"dyad-{args.case}"
        scenario_path = root / "assets" / f"{args.case}.json"
        input_path = root / args.case / "input.md"
        scenario = load_json(scenario_path)
        patch, _paths = validate_submission(repository, submission)
        args.work_root.mkdir(parents=True, exist_ok=True)
        copy_apply(repository, patch, worktree)

        if not dependencies_ready(worktree) and not args.skip_install:
            install = command(
                ["npm", "ci", "--no-audit", "--no-fund", "--prefer-offline"],
                worktree,
                env=install_env(),
            )
            result["commands"].append(install)
            if install["exit_code"] != 0:
                raise PublicRunError("npm ci failed")
            nested = command(
                [
                    "npm",
                    "ci",
                    "--no-audit",
                    "--no-fund",
                    "--prefix",
                    "testing/fake-llm-server",
                ],
                worktree,
                env=install_env(),
                timeout=300,
            )
            result["commands"].append(nested)
            if nested["exit_code"] != 0:
                raise PublicRunError("fake LLM dependency install failed")
        if not dependencies_ready(worktree):
            raise PublicRunError("verified root/fake-server dependencies are absent")

        type_gate = command(["npm", "run", "ts"], worktree)
        result["commands"].append(type_gate)
        if type_gate["exit_code"] != 0:
            raise PublicRunError("authoritative type gate failed")
        regression = command(
            [
                "npm",
                "test",
                "--",
                "src/lib/chatMode.test.ts",
                "src/pro/main/ipc/handlers/local_agent/tools/run_tests.spec.ts",
            ],
            worktree,
        )
        result["commands"].append(regression)
        if regression["exit_code"] != 0:
            raise PublicRunError("focused upstream regression failed")

        probe_target = worktree / "src/ipc/handlers/__tests__/acceptance_benchmark.integration.test.ts"
        fixture_target = worktree / "e2e-tests/fixtures/engine/local-agent/acceptance-benchmark.js"
        probe_target.parent.mkdir(parents=True, exist_ok=True)
        fixture_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "assets/public_probe.integration.test.ts", probe_target)
        shutil.copy2(root / "assets/acceptance-benchmark.js", fixture_target)
        observation_path = args.work_root.resolve() / f"{args.case}-observation.json"
        run = command(
            ["npm", "test", "--", str(probe_target.relative_to(worktree))],
            worktree,
            env=offline_env(
                {
                    "DYAD_BENCHMARK_CASE_FILE": str(scenario_path.resolve()),
                    "DYAD_BENCHMARK_INPUT_FILE": str(input_path.resolve()),
                    "DYAD_BENCHMARK_OBSERVATION_FILE": str(observation_path),
                }
            ),
        )
        result["commands"].append(run)
        if run["exit_code"] != 0 or not observation_path.is_file():
            raise PublicRunError("public production probe failed to execute")
        observation = load_json(observation_path, limit=4 * 1024 * 1024)
        result.update({"valid": bool(observation.get("entry")), "scorecard": score_public(observation, scenario)})
    except (PublicRunError, OSError, ValueError) as exc:
        result["errors"].append(str(exc))

    output = json.dumps(result, indent=2) + "\n"
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    return 0 if result.get("valid") else 2


if __name__ == "__main__":
    raise SystemExit(main())
