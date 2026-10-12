"""Provider-free explicit dispatcher for Dyad v2 readiness smoke.

The production campaign retains its legacy formal command.  This task-local
helper is the reviewed, explicit command constructor for the readiness
profile, and has no subprocess/provider side effects.
"""
from __future__ import annotations
from pathlib import Path

PROFILE = "single-dev-two-round-hidden-smoke-v1"


def build_command(*, python: Path, formal_one_stop: Path, run_dir: Path,
                  hidden_cases_dir: Path, credential_file: Path,
                  harbor: Path, builder_timeout: int = 28800) -> list[str]:
    if builder_timeout <= 0:
        raise ValueError("builder_timeout must be positive")
    return [
        str(python), "-E", "-s", "-B", str(formal_one_stop),
        "--pilot", "--readiness-profile", PROFILE,
        "--run-dir", str(run_dir), "--credential-file", str(credential_file),
        "--max-dev-rounds", "2", "--n-concurrent", "1",
        "--harbor", str(harbor), "--builder-timeout", str(builder_timeout),
        "--hidden-cases-dir", str(hidden_cases_dir),
    ]


def validate_command(command: list[str]) -> dict[str, object]:
    """Validate the exact v2 selector shape without executing it."""
    required = {
        "--pilot": None,
        "--readiness-profile": PROFILE,
        "--max-dev-rounds": "2",
        "--n-concurrent": "1",
    }
    positions = {value: index for index, value in enumerate(command)}
    errors: list[str] = []
    for flag, expected in required.items():
        if flag not in positions:
            errors.append("missing " + flag)
            continue
        if expected is not None:
            index = positions[flag]
            if index + 1 >= len(command) or command[index + 1] != expected:
                errors.append(flag + " must be " + expected)
    if "--run-formal" in positions:
        errors.append("v2 readiness command cannot use --run-formal")
    for flag in ("--run-dir", "--hidden-cases-dir", "--credential-file", "--harbor"):
        if flag not in positions or positions[flag] + 1 >= len(command):
            errors.append("missing " + flag)
    return {"valid": not errors, "errors": errors, "profile": PROFILE,
            "public_cases": ["dev_001"], "required_valid_rounds": 2,
            "hidden_cases": ["test_001"], "max_dev_rounds": 2,
            "n_concurrent": 1}
