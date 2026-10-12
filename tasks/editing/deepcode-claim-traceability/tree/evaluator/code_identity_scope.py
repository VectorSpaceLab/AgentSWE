"""Evaluator-owned DeepCode freeze bridge and auditable source-pack scope.

No Candidate module is imported or executed. Scope selection precedes the size
check; an unrepresentable modification/dependency fails closed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import runpy
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCE = ROOT / "evaluator/harness/candidate_adapter.py"
CREATE_SOURCE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
BASELINE = ROOT / "input/repository"
# Migrated after review: the Candidate's starting repository is what every
# run materializes from, and its digest is stable across every copy of the
# digest algorithm. The superseded pin matched no state used in this
# campaign; the newest baseline file is a documented task repair dated
# 2026-09-03 (see meta/cycle_001_refine_report.md and the compileall
# exclusion in dev_cases/run_public.py).
# Superseded value: 390f9f6d8ea2f3dc9de91c72c7c06c890d1061a831d039306f9068ecf1ad6c1a
BASELINE_DIGEST = "3dfd73e7c9fad2d11f2276c240d25f420430fbbcf073e520648e5fac4f5a0693"
# Migrated after review: the four digest/pack functions this pin protects are
# byte-identical to every earlier copy; the file changed only in judge
# configuration (reasoning effort, and the output ceiling raised 32000 -> 64000
# after two independent answers were shown to be truncated).
# Superseded value: db0188ef413842d1c02798796380e479c0d280cd051074cc85d52ec46e06400b
# Release migration: the released control/code_eval.py differs from the paper copy only in two
# lines that carry the provider gateway alias (a comment and a provider label); the digest/pack
# functions this pin protects are byte-identical.
# Superseded value: 0f16523fc1d1b962e77a16da5cebe1997605f9e6892de79fa21d8a0bd27c9be4
CREATE_SOURCE_SHA256 = "0234d40a884d9f29c3ebd6aed37c8b442f29c7dd67f28838606d0b7520ab1ed7"
NATIVE_SOURCE_SHA256 = "42dcd5e9e8ed242ef7fd504f08037998ff6088f42550ff5d5797ddd1b37f9c10"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def inventory(root):
    """lstat links before reading; never follow a link outside this tree."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root must be a real directory")
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                if not inside(path, root) or not path.exists():
                    raise ValueError("source symlink escapes tree or is dangling: " + relative)
                result[relative] = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISREG(mode):
                result[relative] = {"type": "file", "sha256": sha(path), "bytes": path.stat().st_size}
            elif not stat.S_ISDIR(mode):
                raise ValueError("source special file is not safe to inspect: " + relative)
    return result


def algorithms():
    # These are the unchanged algorithms that originally defined each axis.
    if sha(NATIVE_SOURCE) != NATIVE_SOURCE_SHA256 or sha(CREATE_SOURCE) != CREATE_SOURCE_SHA256:
        raise ValueError("frozen digest algorithm source changed; explicit migration required")
    return runpy.run_path(str(NATIVE_SOURCE)), runpy.run_path(str(CREATE_SOURCE))


def freeze_file(run_dir):
    for relative in ("freeze_manifest.json", "lifecycle/freeze_manifest.json", "controller/freeze_manifest.json"):
        path = Path(run_dir) / relative
        if path.is_file():
            if path.is_symlink() or not inside(path, run_dir):
                raise ValueError("freeze manifest is not an owned regular file")
            return path
    raise ValueError("missing original freeze manifest")


def code_frozen_identity(freeze, run_dir):
    manifest = freeze_file(run_dir)
    if json.loads(manifest.read_text()) != freeze:
        raise ValueError("freeze manifest changed since it was read")
    raw = freeze.get("candidate_path") or freeze.get("repository") or freeze.get("frozen_candidate") or freeze.get("frozen_candidate_path")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("freeze lacks absolute Candidate root")
    candidate = Path(raw)
    if not inside(candidate, run_dir):
        raise ValueError("frozen source is outside this run")
    inventory(candidate)
    native, create = algorithms()
    lifecycle = native["tree_digest"](candidate)
    digests = [freeze[k] for k in ("candidate_materialized_digest", "candidate_digest", "repository_digest", "frozen_digest_before", "frozen_digest_after") if freeze.get(k)]
    if not digests or any(value != lifecycle for value in digests):
        raise ValueError("actual frozen source lifecycle digest mismatch")
    identity = {
        "schema_version": "agentswe-edit-dual-frozen-identity/v1", "valid": True,
        "candidate_path": str(candidate.resolve()), "lifecycle_candidate_digest": lifecycle,
        "code_candidate_digest": create["tree_digest"](candidate),
        "freeze_sha256": sha(manifest),
        "algorithms": {
            "lifecycle": {"source": str(NATIVE_SOURCE), "sha256": sha(NATIVE_SOURCE), "function": "tree_digest", "includes_directories": True, "excludes_generated_caches": True},
            "code": {"source": str(CREATE_SOURCE), "sha256": sha(CREATE_SOURCE), "function": "tree_digest", "includes_directories": False, "excludes_generated_caches": False},
        },
    }
    # Once established, the immutable bridge is also the original manifest
    # binding for Result-only/cached publication. Never adopt a rewritten freeze
    # or a changed full-tree cache payload under the old lifecycle identity.
    prior = Path(run_dir) / "formal_scoring/code_axis/code_frozen_identity.json"
    if prior.exists() or prior.is_symlink():
        if prior.is_symlink() or not inside(prior, run_dir) or not prior.is_file():
            raise ValueError("existing Code bridge is not an owned regular file")
        if json.loads(prior.read_text()) != identity:
            raise ValueError("immutable Code/lifecycle bridge or original freeze changed")
    return identity


def frozen_identity_errors(freeze, run_dir):
    try:
        code_frozen_identity(freeze, run_dir)
        return []
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return ["DeepCode frozen identity: " + str(exc)]


def optional(path):
    p = Path(path)
    # All Python and desktop release/sidecar scripts are retained, including
    # scripts dynamically loaded by the retained regression tests.
    return (p.parts[0].startswith(".") or
            (p.parts[0] in {"desktop", "docs"} and p.suffix != ".py" and p.parts[:2] != ("desktop", "scripts")))


def dependencies(candidate, selected, entries, create):
    """Expand Python imports and literal local file references without exec.

    All backend source/data/tests are already seeds. Nonliteral dynamic calls
    are disclosed; their optional component must be included if touched by the
    Candidate, rather than declaring AST imports a complete runtime proof.
    """
    modules = {}
    for relative in entries:
        if relative.endswith(".py"):
            parts = list(Path(relative).with_suffix("").parts)
            if parts[-1] == "__init__": parts.pop()
            modules[".".join(parts)] = relative
    graph, dynamic, parse_errors = {}, [], []
    by_basename = {}
    for relative in entries:
        by_basename.setdefault(Path(relative).name, []).append(relative)
    queue = sorted(selected)
    visited = set()
    while queue:
        relative = queue.pop()
        if relative in visited: continue
        visited.add(relative)
        path = candidate / relative
        if entries[relative]["type"] == "symlink":
            target = path.resolve().relative_to(candidate.resolve()).as_posix()
            targets = {item for item in entries if item == target or item.startswith(target + "/")}
        elif path.suffix == ".py":
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (SyntaxError, UnicodeError) as exc:
                parse_errors.append({"path": relative, "error": type(exc).__name__})
                continue
            targets = set()
            package = list(Path(relative).parent.parts)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import): names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = package[:len(package) - node.level + 1] if node.level else []
                    module = ".".join(prefix + ([node.module] if node.module else []))
                    names = [module] + [module + "." + alias.name for alias in node.names]
                for name in names:
                    parts = name.split(".")
                    for length in range(1, len(parts) + 1):
                        matched = modules.get(".".join(parts[:length]))
                        if matched: targets.add(matched)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value
                    if value in entries: targets.add(value)
                    sibling = (Path(relative).parent / value).as_posix()
                    if sibling in entries: targets.add(sibling)
                    # A unique bare filename also identifies a local fixture.
                    # Ambiguous README/package names are not resolved to every
                    # unrelated frontend example merely because names match.
                    if "/" not in value and Path(value).suffix and len(by_basename.get(value, [])) == 1:
                        targets.update(by_basename[value])
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    if name in {"__import__", "importlib.import_module", "importlib.util.spec_from_file_location"}:
                        dynamic.append({"path": relative, "line": node.lineno, "call": ast.unparse(node)[:400]})
        else:
            targets = set()
        graph[relative] = sorted(targets)
        for target in targets - selected:
            selected.add(target)
            queue.append(target)
    return graph, dynamic, parse_errors


def prepare_code_evidence_scope(candidate, digest, output_dir):
    candidate = Path(candidate).absolute()
    current = inventory(candidate)
    baseline = inventory(BASELINE)
    _, create = algorithms()
    observed = create["tree_digest"](BASELINE)
    if observed != BASELINE_DIGEST:
        raise ValueError("public baseline changed from pinned full-tree digest")
    if create["tree_digest"](candidate) != digest:
        raise ValueError("Code scope Candidate digest mismatch")
    modified = sorted(p for p in current.keys() & baseline.keys() if current[p] != baseline[p])
    added = sorted(current.keys() - baseline.keys())
    deleted = sorted(baseline.keys() - current.keys())
    # Bytecode and other build artefacts appear as "added" whenever the
    # evaluation runs the code inside the frozen tree, and Create's SKIP_PARTS
    # already excludes them from the base selection below. Without the same
    # filter here, `selected.update(changes)` puts every one of them back and the
    # exclusion means nothing.
    changes = {p for p in set(modified + added)
               if not any(part in create["SKIP_PARTS"] for part in Path(p).parts)}
    # Scope decisions use mechanisms and exact changes, never measured sizes.
    selected = {p for p in current if not optional(p) and not any(part in create["SKIP_PARTS"] for part in Path(p).parts)}
    selected.update(changes)
    touched_optional = {Path(p).parts[0] for p in modified + added + deleted if Path(p).parts[0] in {"desktop", "docs"}}
    selected.update(p for p in current if Path(p).parts[0] in touched_optional and not any(part in create["SKIP_PARTS"] for part in Path(p).parts))
    graph, dynamic, parse_errors = dependencies(candidate, selected, current, create)
    errors = []
    dependency_pack_exclusions = []
    # This is an unchanged third-party Node distribution lock, not implementation
    # of the public Python CLI. Retain its package declarations, exact lock hash,
    # Python sidecar lock, license-audit implementation, all Python release
    # checks and tests. Any Candidate change to this component or to a referring
    # file disables the exception; new non-Python changes require full review.
    lock = "desktop/package-lock.json"
    referrers = sorted(p for p, values in graph.items() if lock in values)
    if (lock in selected and current.get(lock) == baseline.get(lock)
            and "desktop" not in touched_optional
            and not (set(referrers) & changes)
            and all(Path(p).suffix == ".py" for p in changes)):
        selected.remove(lock)
        dependency_pack_exclusions.append({"path": lock, **current[lock],
            "referenced_by": referrers,
            "retained_dependency_metadata": ["desktop/package.json", "desktop/sidecar-requirements.lock", "desktop/src-tauri/Cargo.toml"],
            "reason": "Unchanged optional desktop third-party Node distribution lock. Public task requires offline Python CLI and excludes a web application. Full package declarations, Python sidecar pins, release/license-audit code and tests remain; full lock bytes remain in the canonical frozen digest. No Candidate-authored or modified dependency is excluded."})
    # Exact per-file paths avoid claiming a directory included files which the
    # authoritative packer actually skipped. Dot-prefix normalization and binary
    # omissions are checked against the actual pack manifest below.
    evidence_paths = sorted(selected)
    try:
        manifest, pack = create["source_manifest_and_pack"](candidate, max_pack_bytes=create["MAX_SOURCE_PACK_BYTES"], evidence_paths=evidence_paths)
    except ValueError as exc:
        raise ValueError("justified Code scope cannot fit unchanged Create policy: " + str(exc)) from exc
    included = {item["path"] for item in manifest["files"] if item.get("included_in_evidence_pack")}
    selected_unrepresented = sorted(selected - included)
    for path in selected_unrepresented:
        entry = current[path]
        # Unchanged binary assets are hash-bound metadata; changed binary/code,
        # ignored caches, and links cannot silently pass as full change evidence.
        unchanged_dev_metadata = Path(path).parts[0].startswith(".") and entry == baseline.get(path)
        if path in changes or entry["type"] == "symlink" or ((candidate / path).suffix in create["TEXT_SUFFIXES"] and not unchanged_dev_metadata):
            errors.append("required evidence not represented by Create text pack: " + path)
    if parse_errors:
        # A syntax error is visible to Code; conservatively include all optional
        # surfaces before claiming scope completeness when dependency AST fails.
        errors.append("dependency parse incomplete; explicit full-scope review required")
    omitted = sorted(set(current) - included)
    exclusions = [{"path": p, **current[p], "reason": (
        "unchanged generated/runtime metadata excluded by authoritative Create pack policy" if any(x in create["SKIP_PARTS"] for x in Path(p).parts)
        else "unchanged optional frontend/docs/development metadata; not changed or statically referenced by retained Python mechanisms" if optional(p)
        else "unchanged binary asset retained in complete frozen tree and metadata")}
        for p in omitted if p not in changes]
    scope = {
        "schema_version": "agentswe-edit-code-scope/v1", "valid": not errors, "errors": errors,
        "candidate_digest": digest, "baseline_expected_digest": BASELINE_DIGEST, "baseline_observed_digest": observed,
        "scope_basis": "Public offline cross-process CLI requirements; complete backend, legacy workflows, all Python and regression tests, root packaging/locks, assets/prompts/schema/protocol; exact Candidate changes and local dependency expansion.",
        "selection_policy": "Select by mechanism and full byte comparison before cap validation. Changed optional component is retained in full. No source, rubric or Create cap is changed. Any unrepresented change/dependency fails closed.",
        "complete_change_coverage": not (changes - included),
        # Reported from the same selection the evidence is built from: bytecode
        # and other artefacts the evaluation itself produced are not Candidate
        # source, and Create's SKIP_PARTS already says so. What is dropped is
        # listed below rather than disappearing.
        "changed_paths": [p for p in modified if p in changes],
        "added_paths": [p for p in added if p in changes],
        "deleted_paths": deleted,
        "generated_change_exclusions": sorted(set(modified + added) - changes),
        "evidence_paths": evidence_paths, "dependency_paths": sorted({p for values in graph.values() for p in values}),
        "mandatory_included_paths": sorted(included), "mandatory_excluded_paths": [p for p in selected_unrepresented if p in changes],
        "unchanged_optional_dependency_exclusions": exclusions,
        "unchanged_dependency_pack_exclusions": dependency_pack_exclusions,
        "dynamic_import_calls": sorted(dynamic, key=lambda v: (v["path"], v["line"])),
        "dependency_parse_errors": parse_errors,
        "dependency_review_limits": "Arbitrary nonliteral file accesses cannot be proved by AST analysis. Entire backend/data and Python release scripts are retained conservatively. Changed optional components are retained entirely; dynamic sites are disclosed for semantic Code review.",
        "source_pack_bytes": len(pack.encode("utf-8")), "source_pack_sha256": manifest["evidence_pack_sha256"],
        "source_pack_cap_bytes": create["MAX_SOURCE_PACK_BYTES"],
        "source_scope_algorithm": {"path": str(Path(__file__).resolve()), "sha256": sha(__file__)},
        "public_requirement_sha256": {p.name: sha(p) for p in sorted((ROOT / "input").glob("*.md"))},
    }
    # Recheck sources after all inspections. The shared finalizer persists this
    # deterministic object immutably and rechecks the complete freeze at end.
    if create["tree_digest"](candidate) != digest or create["tree_digest"](BASELINE) != observed:
        raise ValueError("source changed during Code scope inspection")
    return scope
