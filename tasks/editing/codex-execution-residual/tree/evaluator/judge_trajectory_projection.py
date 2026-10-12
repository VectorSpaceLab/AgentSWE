#!/usr/bin/env python3
"""Bounded judge projection of a Codex JSONL trajectory (decision D6, 2026-09-19).

The Candidate product under test may legitimately print very large tool output
(the task's theme is execution residual / truncation policy), and a hidden-case
trajectory has reached 3.98 MB (~1.45M tokens), which exceeds the judge model's
context.  Raising the judge's byte limit would only move the failure to the
provider.  This module produces a *projection* for the Result judge:

  * every JSONL record is kept, in order, with all keys;
  * only string values longer than ``KEEP_HEAD + KEEP_TAIL`` bytes are replaced by
    ``head + marker + tail`` where the marker states the original length, the
    sha256 of the original string and how many bytes were omitted — the judge
    therefore *sees that* the Candidate produced N bytes at that point;
  * nothing else is altered; no records are dropped; the original file is left
    untouched on disk as evidence.

The projection is written next to the original as ``<name>.judge-projection.jsonl``
together with ``<name>.judge-projection.json`` recording original/projected sizes,
sha256 of both files, the rule constants and the list of truncated fields, so the
score contract can cite exactly what the judge was shown.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

KEEP_HEAD = 8_000
KEEP_TAIL = 2_000
RULE = "agentswe-codex-judge-trajectory-projection/v1"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(value, path: str, truncated: list):
    if isinstance(value, dict):
        return {k: _project(v, f"{path}.{k}", truncated) for k, v in value.items()}
    if isinstance(value, list):
        return [_project(v, f"{path}[{i}]", truncated) for i, v in enumerate(value)]
    if isinstance(value, str) and len(value) > KEEP_HEAD + KEEP_TAIL:
        raw = value.encode("utf-8", "surrogatepass")
        omitted = len(value) - KEEP_HEAD - KEEP_TAIL
        marker = (f"\n[evaluator truncation for judge projection: original string {len(value)} chars / "
                  f"{len(raw)} bytes, sha256 {_sha256_bytes(raw)}; {omitted} chars omitted here; "
                  f"the Candidate really emitted the full content]\n")
        truncated.append({"path": path, "original_chars": len(value), "original_bytes": len(raw),
                          "sha256": _sha256_bytes(raw), "omitted_chars": omitted})
        return value[:KEEP_HEAD] + marker + value[-KEEP_TAIL:]
    return value


MAX_JOIN_LINES = 64  # physical lines one split Candidate record may span


def project(source: Path) -> tuple[Path, Path]:
    source = Path(source)
    original = source.read_bytes()
    lines = original.decode("utf-8", "surrogatepass").splitlines()
    out_lines = []
    truncated: list = []
    records = 0
    malformed: list = []
    joined: list = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        records += 1
        row = None
        consumed = 1
        # A Candidate record whose string content carried raw newline bytes arrives split
        # across physical lines: re-join forward (bounded) before treating it as malformed.
        for extra in range(0, MAX_JOIN_LINES + 1):
            chunk = "\n".join(lines[index:index + 1 + extra])
            try:
                row = json.loads(chunk, strict=False)
            except ValueError:
                continue
            consumed = 1 + extra
            break
        if row is None:
            raw = line.encode("utf-8", "surrogatepass")
            entry = {"line": index, "original_chars": len(line), "original_bytes": len(raw),
                     "sha256": _sha256_bytes(raw)}
            malformed.append(entry)
            out_lines.append(json.dumps({"evaluator_projection": "malformed_candidate_record",
                                         "note": "the Candidate emitted a trajectory record that is not valid JSON; "
                                                 "the original bytes are retained in the source file",
                                         **entry, "head": line[:KEEP_HEAD]}, ensure_ascii=False))
        else:
            if consumed > 1:
                joined.append({"line": index, "physical_lines": consumed})
            out_lines.append(json.dumps(_project(row, f"[{index}]", truncated), ensure_ascii=False))
        index += consumed
    projected_text = ("\n".join(out_lines) + "\n").encode("utf-8", "surrogatepass")
    destination = source.with_name(source.name + ".judge-projection.jsonl")
    manifest_path = source.with_name(source.name + ".judge-projection.json")
    destination.write_bytes(projected_text)
    manifest = {
        "schema_version": RULE,
        "source": str(source), "source_bytes": len(original), "source_sha256": _sha256_bytes(original),
        "projection": str(destination), "projection_bytes": len(projected_text),
        "projection_sha256": _sha256_bytes(projected_text),
        "records": records, "records_dropped": 0,
        "malformed_records": malformed, "joined_records": joined,
        "rule": {"keep_head_chars": KEEP_HEAD, "keep_tail_chars": KEEP_TAIL,
                 "applies_to": "string values longer than keep_head+keep_tail; all keys and records retained"},
        "truncated_fields": truncated,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination, manifest_path


if __name__ == "__main__":
    dest, man = project(Path(sys.argv[1]))
    m = json.loads(man.read_text())
    print(f"{m['source_bytes']} -> {m['projection_bytes']} bytes, records {m['records']}, truncated fields {len(m['truncated_fields'])}")
