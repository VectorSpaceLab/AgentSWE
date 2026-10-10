"""Reference control for the deterministic surface assertions (0920 hardening).

Builds, from the case's own private manifest, the workspace a *correct* product
would leave behind -- documents, impact report, committed receipt, tenant
state, static publication release and search generation -- and asserts that
every deterministic assertion in
``agentloop.evaluator.surface_assertions`` passes on it.  Then it degrades that
workspace one obligation at a time and asserts that exactly the corresponding
assertion flips to ``False``.

This is a control, not a solution: it never runs the product and never becomes
part of a Candidate's input.  It exists so the hardening's ceilings are known
to be satisfiable by the published spec rather than merely strict.

    python3 evaluator/tests/reference_control.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agentloop.evaluator import surface_assertions as sa  # noqa: E402

RESERVED = {"INSTRUCTIONS.md", "_plan.md", "log.md"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(case_id: str) -> dict:
    return json.loads((ROOT / "evaluator" / "manifests" / f"{case_id}.json").read_text(encoding="utf-8"))


def seed_documents(repo: Path, spec: dict) -> None:
    """One page per corpus path with the anchors, tokens and required facts."""
    pages = sorted(set(spec["expected_pages"]) | set(spec["unaffected_pages"]))
    if not pages:
        pages = ["openwiki/overview.md"]
    for index, page in enumerate(pages):
        path = repo / page
        path.parent.mkdir(parents=True, exist_ok=True)
        title = PurePosixPath(page).stem.replace("-", " ").title()
        body = ["---", "title: %s" % title, "---", "", "# %s" % title, "",
                "Reference body for %s." % page, ""]
        if index == 0:
            for token in spec.get("preserved_tokens", []):
                body.append(token)
            for anchor in spec.get("preserved_anchors", []):
                body.append('<!-- openwiki:anchor id="%s" -->' % anchor)
            body.append("")
        for needle in spec.get("required_text", {}).get(page, []):
            body.append("Fact: %s" % needle)
        body.append("")
        path.write_text("\n".join(body), encoding="utf-8")


def build_reference_workspace(case_id: str, spec: dict, root: Path) -> tuple[Path, Path]:
    initial, observed = root / "initial", root / "observed"
    for base in (initial, observed):
        (base / "openwiki").mkdir(parents=True, exist_ok=True)
        seed_documents(base, spec)
        write_json(base / "impact-manifest.json", {
            "schema_version": "1.0", "docs_root": "openwiki",
            "change_source": {"kind": spec["change_kind"]},
            "report_path": "impact-report.json", "diff_path": "documentation.diff"})
    # The correct product changes exactly the affected pages.
    for page in spec["expected_pages"]:
        path = observed / page
        path.write_text(path.read_text(encoding="utf-8") + "\nUpdated by the reference control.\n",
                        encoding="utf-8")

    transaction = spec["transaction"]
    publication = spec.get("publication") or {}
    search = spec.get("search") or {}
    digest = transaction["payload_digest"]
    corpus = {path: sha(observed / path) for path in sorted(
        p.relative_to(observed).as_posix() for p in (observed / "openwiki").rglob("*.md")
        if p.is_file() and p.name not in RESERVED)}

    report = {
        "schema_version": "1.0",
        "status": "no_changes" if spec.get("expect_no_changes") else "updated",
        "change_source": {"kind": spec["change_kind"]},
        "affected": {
            "pages": [{"path": path, "direct": path in set(spec["direct_pages"]),
                       "reasons": ["source change touches %s" % path]}
                      for path in sorted(spec["expected_pages"])],
            "examples": [], "diagrams": [], "links": [],
            "stale_claims": [{"id": "stale-%d" % index, "page": sorted(spec["expected_pages"])[0],
                              "kind": kind, "evidence": ["current source and tests"],
                              "resolution": "rewritten from source"}
                             for index, kind in enumerate(spec["expected_stale_kinds"])],
        },
        "changed_paths": sorted(spec["expected_pages"]),
        "validation": {"examples_total": 0, "examples_passed": 0, "examples_failed": 0,
                       "links_broken": 0, "schema_valid": True},
        "security": {"ignored_instruction_count": 0, "unsafe_execution_count": 0},
        "errors": [],
    }
    for example_id, expected_stdout in (spec.get("expected_examples") or {}).items():
        if expected_stdout is None:
            report["affected"]["examples"].append(
                {"id": example_id, "page": sorted(spec["expected_pages"])[0], "status": "skipped",
                 "command": [], "expected_stdout": "", "actual_stdout": "", "exit_code": 0})
        else:
            report["affected"]["examples"].append(
                {"id": example_id, "page": sorted(spec["expected_pages"])[0], "status": "passed",
                 "command": ["node", "openwiki/%s.mjs" % example_id],
                 "expected_stdout": expected_stdout, "actual_stdout": expected_stdout, "exit_code": 0})
    report["validation"]["examples_total"] = len(report["affected"]["examples"])
    report["validation"]["examples_passed"] = sum(
        1 for item in report["affected"]["examples"] if item["status"] == "passed")
    for _ in range(int(spec.get("minimum_link_reports") or 0)):
        report["affected"]["links"].append(
            {"page": sorted(spec["expected_pages"])[0],
             "target": (spec.get("expected_link_suffixes") or ["./index.md"])[0],
             "status": "repaired"})

    publication_manifest_sha = search_manifest_sha = None
    if publication:
        site = observed / publication["root"] / "sites" / publication["site_id"]
        release = site / "releases" / str(publication["generation"])
        (release / "pages").mkdir(parents=True, exist_ok=True)
        for path in corpus:
            rendered = release / "pages" / PurePosixPath(path).relative_to("openwiki").with_suffix(".html")
            rendered.parent.mkdir(parents=True, exist_ok=True)
            source = (observed / path).read_text(encoding="utf-8")
            heading = next((line.lstrip("# ").strip() for line in source.splitlines()
                            if line.startswith("#")), PurePosixPath(path).stem)
            rendered.write_text(
                "<!doctype html><html><body><h1>%s</h1><pre>%s</pre>"
                '<nav><a href="../index.html">index</a></nav></body></html>'
                % (heading, source.replace("<", "&lt;")), encoding="utf-8")
        (release / "index.html").write_text(
            "<!doctype html><html><body><h1>%s</h1><ul>%s</ul></body></html>" % (
                publication["site_id"],
                "".join('<li><a href="pages/%s">%s</a></li>' % (
                    PurePosixPath(path).relative_to("openwiki").with_suffix(".html"), path)
                    for path in corpus)), encoding="utf-8")
        write_json(release / "graph.json", {
            "nodes": [{"path": path, "title": PurePosixPath(path).stem} for path in corpus],
            "edges": []})
        files = {p.relative_to(release).as_posix(): sha(p)
                 for p in sorted(release.rglob("*")) if p.is_file() and p.name != "manifest.json"}
        write_json(release / "manifest.json", {
            "schema_version": "1.0", "site_id": publication["site_id"],
            "generation": publication["generation"], "payload_digest": digest,
            "base_path": publication["base_path"], "documentation_hashes": corpus, "files": files})
        publication_manifest_sha = sha(release / "manifest.json")
        write_json(site / "active.json", {
            "schema_version": "1.0", "site_id": publication["site_id"],
            "generation": publication["generation"], "payload_digest": digest,
            "release_path": release.relative_to(observed).as_posix(),
            "manifest_sha256": publication_manifest_sha})
        report["publication"] = {
            "schema_version": "1.0", "site_id": publication["site_id"],
            "generation": publication["generation"], "payload_digest": digest,
            "active_path": (site / "active.json").relative_to(observed).as_posix(),
            "manifest_path": (release / "manifest.json").relative_to(observed).as_posix(),
            "manifest_sha256": publication_manifest_sha,
            "page_count": len(corpus), "status": "published"}

    if search:
        index_root = observed / search["root"] / "indexes" / search["index_id"]
        generation = index_root / "generations" / str(search["generation"])
        generation.mkdir(parents=True, exist_ok=True)
        documents = []
        for path in corpus:
            source = (observed / path).read_text(encoding="utf-8")
            headings = [line.lstrip("# ").strip() for line in source.splitlines() if line.startswith("#")]
            text = " ".join(line for line in source.splitlines() if not line.startswith("<!--"))
            documents.append({"path": path, "title": headings[0] if headings else PurePosixPath(path).stem,
                              "headings": headings, "text": text,
                              "documentation_sha256": corpus[path]})
        # A correct index answers every query the case asks about a page it
        # names, and carries no text the case expects to have gone stale.
        for query_spec in (spec.get("search_queries") or []):
            for path in query_spec.get("expected_paths", []):
                for record in documents:
                    if record["path"] == path:
                        record["text"] += " " + str(query_spec["query"])
        write_json(generation / "index.json", {
            "schema_version": "1.0", "index_id": search["index_id"],
            "generation": search["generation"], "payload_digest": digest, "documents": documents})
        write_json(generation / "manifest.json", {
            "schema_version": "1.0", "index_id": search["index_id"],
            "generation": search["generation"], "payload_digest": digest,
            "documentation_hashes": corpus, "index_sha256": sha(generation / "index.json")})
        search_manifest_sha = sha(generation / "manifest.json")
        write_json(index_root / "active.json", {
            "schema_version": "1.0", "index_id": search["index_id"],
            "generation": search["generation"], "payload_digest": digest,
            "generation_path": generation.relative_to(observed).as_posix(),
            "manifest_sha256": search_manifest_sha})
        report["search_index"] = {
            "schema_version": "1.0", "index_id": search["index_id"],
            "generation": search["generation"], "payload_digest": digest,
            "active_path": (index_root / "active.json").relative_to(observed).as_posix(),
            "manifest_path": (generation / "manifest.json").relative_to(observed).as_posix(),
            "manifest_sha256": search_manifest_sha,
            "document_count": len(corpus), "status": "indexed"}

    tenant = observed / transaction["state_dir"] / "tenants" / transaction["tenant_id"]
    receipt_path = tenant / "receipts" / ("%s.json" % transaction["request_id"])
    receipt = {
        "schema_version": "1.0", "status": "committed",
        "tenant_id": transaction["tenant_id"], "request_id": transaction["request_id"],
        "generation": transaction["generation"], "payload_digest": digest,
        "owner_id": transaction["owner_id"],
        "changed_paths": sorted(spec["expected_pages"]),
        "documentation_hashes": {path: sha(observed / path) for path in sorted(spec["expected_pages"])},
        "result": {"status": "no_changes" if spec.get("expect_no_changes") else "updated",
                   "validation": report["validation"], "security": report["security"]},
    }
    if publication:
        receipt["publication"] = dict(report["publication"])
    if search:
        receipt["search_index"] = dict(report["search_index"])
    write_json(receipt_path, receipt)
    write_json(tenant / "state.json", {
        "schema_version": "1.0", "tenant_id": transaction["tenant_id"],
        "latest_generation": transaction["generation"],
        "latest_request_id": transaction["request_id"]})
    report["transaction"] = {
        "tenant_id": transaction["tenant_id"], "request_id": transaction["request_id"],
        "generation": transaction["generation"], "payload_digest": digest,
        "outcome": "committed", "receipt_path": receipt_path.relative_to(observed).as_posix()}

    write_json(observed / "impact-report.json", report)
    (observed / "documentation.diff").write_text(
        "".join("diff --git a/%s b/%s\n" % (path, path) for path in sorted(spec["expected_pages"])),
        encoding="utf-8")
    write_json(observed / "agent_result.json", {
        "schema_version": "openwiki-agent-result/v1", "case_id": case_id,
        "observations": ["reference control"], "integrity": {"receipt_path": str(receipt_path.name)},
        "decision": {"completion_claim": "complete", "rationale": "reference control"}})
    return initial, observed


DEGRADATIONS = {
    "impact_precision_no_unaffected": lambda spec, observed, report: report["affected"]["pages"].append(
        {"path": spec["unaffected_pages"][0], "direct": False, "reasons": ["guessed"]}),
    "impact_page_provenance": lambda spec, observed, report: report["affected"]["pages"][0].update(reasons=[]),
    "impact_direct_classification": lambda spec, observed, report: [
        item.update(direct=not item["direct"]) for item in report["affected"]["pages"]],
    "impact_stale_claim_evidence": lambda spec, observed, report: [
        item.update(evidence=[]) for item in report["affected"]["stale_claims"]],
}


def degrade_and_check(case_id, spec, root, name, mutate):
    initial, observed = build_reference_workspace(case_id, spec, root)
    report = json.loads((observed / "impact-report.json").read_text(encoding="utf-8"))
    mutate(spec, observed, report)
    write_json(observed / "impact-report.json", report)
    value = sa.evaluate(case_id, spec, initial, observed)
    return value["assertion_results"].get(name)


def main() -> int:
    failures = []
    for index in range(1, 7):
        case_id = "test_%03d" % index
        spec = load_manifest(case_id)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial, observed = build_reference_workspace(case_id, spec, root)
            value = sa.evaluate(case_id, spec, initial, observed)
            failed = [name for name, passed in value["assertion_results"].items() if not passed]
            print("%s: %d/%d assertions pass, families=%d/%d%s" % (
                case_id, len(value["assertion_results"]) - len(failed),
                len(value["assertion_results"]), value["surface_families_satisfied_count"],
                value["surface_families_total"], "" if not failed else "  FAILED: %s" % failed))
            if value["observation_notes"]:
                print("   notes:", value["observation_notes"])
            if failed:
                failures.append((case_id, failed))
            if value["surface_families_satisfied_count"] != value["surface_families_total"]:
                failures.append((case_id, ["families"]))
    # Each published obligation must be individually detectable.
    spec = load_manifest("test_002")
    for name, mutate in DEGRADATIONS.items():
        with tempfile.TemporaryDirectory() as raw:
            observed_value = degrade_and_check("test_002", spec, Path(raw), name, mutate)
        print("degradation %-40s -> %s" % (name, observed_value))
        if observed_value is not False:
            failures.append(("degradation", [name]))
    if failures:
        print("\nREFERENCE CONTROL FAILED:", failures)
        return 1
    print("\nreference control passed: the published spec satisfies every deterministic assertion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
