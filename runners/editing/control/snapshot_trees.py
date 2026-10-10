#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


PAIRS = {
    "claude": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/11-edit-claude-policy-provenance-v3"),
        Path("@@AGENTSWE_EDITING_TASKS@@/claude-policy-provenance/tree"),
    ),
    "aider": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/12-edit-aider-worktree-transaction"),
        Path("@@AGENTSWE_EDITING_TASKS@@/aider-worktree-transaction/tree"),
    ),
    "openhands": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/13-edit-openhands-effect-recovery"),
        Path("@@AGENTSWE_EDITING_TASKS@@/openhands-effect-recovery/tree"),
    ),
    "openclaw": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/14-edit-openclaw-channel-handoff"),
        Path("@@AGENTSWE_EDITING_TASKS@@/openclaw-channel-handoff/tree"),
    ),
    "codex": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/15-edit-codex-execution-residual-v3"),
        Path("@@AGENTSWE_EDITING_TASKS@@/codex-execution-residual/tree"),
    ),
    "ai-scientist": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/16-edit-ai-scientist-reproducibility-gate-v5"),
        Path("@@AGENTSWE_EDITING_TASKS@@/ai-scientist-reproducibility-gate/tree"),
    ),
    "deepcode": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/17-edit-deepcode-claim-traceability"),
        Path("@@AGENTSWE_EDITING_TASKS@@/deepcode-claim-traceability/tree"),
    ),
    "deeptutor": (
        Path("@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/a0src"),
        Path("@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/tree"),
    ),
    "dyad": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/19-edit-dyad-acceptance-driven"),
        Path("@@AGENTSWE_EDITING_TASKS@@/dyad-acceptance-driven/tree"),
    ),
    "openwiki": (
        Path("@@AGENTSWE_EDITING_SOURCES@@/20-edit-openwiki-change-impact"),
        Path("@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree"),
    ),
}
SKIP_PARTS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".runtime", ".formal_runs", "artifacts", "outputs",
}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def tree_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = 0
    total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_PARTS)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if any(part in SKIP_PARTS for part in Path(relative).parts) or path.suffix in SKIP_SUFFIXES:
                continue
            rel = relative.encode("utf-8")
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8")
                digest.update(b"L")
                digest.update(len(rel).to_bytes(8, "big"))
                digest.update(rel)
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
                files += 1
                total += len(payload)
            elif path.is_file():
                digest.update(b"F")
                digest.update(len(rel).to_bytes(8, "big"))
                digest.update(rel)
                size = path.stat().st_size
                digest.update(size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                files += 1
                total += size
    return digest.hexdigest(), files, total


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = {}
    for task, (source, sibling) in PAIRS.items():
        source_digest, source_files, source_bytes = tree_digest(source)
        sibling_digest, sibling_files, sibling_bytes = tree_digest(sibling)
        tasks[task] = {
            "source": {"path": str(source), "digest": source_digest, "files": source_files, "bytes": source_bytes},
            "sibling": {"path": str(sibling), "digest": sibling_digest, "files": sibling_files, "bytes": sibling_bytes},
        }
    value = {
        "schema_version": "agentswe-edit-tree-snapshot-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "digest_exclusions": sorted(SKIP_PARTS),
        "tasks": tasks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps({task: item["source"]["digest"] for task, item in tasks.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
