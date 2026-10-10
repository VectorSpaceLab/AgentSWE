"""Deterministic, read-only surface assertions for the Agent-loop Result oracle.

Every check in this module is computed from bytes the product itself persisted
into the case workspace.  Nothing here executes the product, runs an example,
issues a search query, repairs a document or supplies an expected answer: the
oracle stays observational, exactly as ``semantic_oracle`` promises.

The checks restate published obligations only:

* ``input/03`` change-impact ownership -- the report must name every affected
  page, must not claim an unaffected one, must separate direct from transitive
  effects, and must carry concise provenance for each claim it makes.
* ``input/02`` durable transaction / static publication / full-text search
  contracts -- stable layouts, identity, hash evidence, offline-safe bytes,
  normalized index records and stale-term removal.
* ``input/02`` cross-surface commit -- one generation and one payload digest
  across receipt, publication and index, or nothing at all.
* ``input/03`` implementation constraints -- writes only under manifest
  authorized roots.

The task's own public harness (``dev_cases/harness_support.py``) already
implements the same obligations for the public dev runner; the logic below is
the read-only subset of it, so the public and hidden paths agree.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath

RESERVED_DOC_NAMES = frozenset({"INSTRUCTIONS.md", "_plan.md", "log.md"})
MAX_WORKSPACE_ENTRIES = 20000
UNSAFE_HTML = re.compile(r"(?is)<script\b|\son[a-z]+\s*=|javascript:|https?://|//cdn\.|file:|\x1b")
STALE_KIND_ALIASES = {
    "symbol-rename": ("rename", "renamed"),
    "default-behavior": ("default", "behavior"),
    "module-move": ("move", "module", "relocat"),
    "compatibility-alias": ("compat", "alias", "re-export"),
    "return-type": ("type", "representation"),
    "transitive-claim": ("transitive", "dependent"),
    "removed-api": ("remov", "delet"),
    "deprecated-api": ("deprecat",),
    "migration-guidance": ("migration", "migrate"),
    "diagram-edge": ("diagram", "mermaid", "edge"),
    "broken-link": ("link", "anchor"),
    "example-output": ("example", "output", "stdout"),
}


# ---------------------------------------------------------------- primitives

def _safe_relative(raw):
    relative = PurePosixPath(str(raw).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe relative path: " + str(raw))
    return relative


def _resolved(root, relative):
    """Join under ``root`` refusing traversal and symlinked path components."""
    root = Path(root)
    relative = _safe_relative(relative)
    for index in range(1, len(relative.parts) + 1):
        if root.joinpath(*relative.parts[:index]).is_symlink():
            raise ValueError("symlinked path component: " + str(relative))
    path = root / relative
    path.resolve().relative_to(root.resolve())
    return path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 8_000_000:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _text(path, limit=4_000_000):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _document_hashes(repo):
    """Every file under openwiki/, including reserved scaffolding, for diffing."""
    result = {}
    root = Path(repo) / "openwiki"
    if not root.is_dir() or root.is_symlink():
        return result
    for index, path in enumerate(sorted(root.rglob("*"))):
        if index >= MAX_WORKSPACE_ENTRIES:
            break
        if path.is_symlink() or not path.is_file():
            continue
        try:
            result[path.relative_to(repo).as_posix()] = _sha(path)
        except (OSError, ValueError):
            continue
    return result


def _corpus_hashes(repo):
    """Allowed wiki corpus exactly as the publication/search contracts define it."""
    result = {}
    root = Path(repo) / "openwiki"
    if not root.is_dir() or root.is_symlink():
        return result
    for path in sorted(root.rglob("*.md")):
        if path.name in RESERVED_DOC_NAMES or path.is_symlink() or not path.is_file():
            continue
        try:
            result[path.relative_to(repo).as_posix()] = _sha(path)
        except (OSError, ValueError):
            continue
    return result


# 0921: the spec pins a literal `schema_version` only inside the JSON blocks it
# actually prints -- the impact manifest (input/02 "Cycle 001" block), the
# `search` command stdout, and the impact report core schema (input/03).  The
# durable/publication/search artifacts are described in prose ("record schema",
# "contains the schema"), so requiring the exact string "1.0" on them scored a spelling
# choice rather than a behaviour: the 0920 run wrote
# `"schema_version": "openwiki-publication/v1"` (and `"schema"` in the report
# evidence blocks) with every identity field correct and still failed
# publication_identity, search_identity and both report-evidence assertions.
# input/02 now states the rule these helpers enforce: declare the schema under
# either published field name with any non-empty string, and keep every
# identity field exact.
def _schema_declared(value):
    if not isinstance(value, dict):
        return False
    return any(isinstance(value.get(key), str) and value.get(key).strip()
               for key in ("schema_version", "schema"))


def _any_field(value, names):
    """Value of the first published spelling present; the spec names the fact, not the key."""
    if not isinstance(value, dict):
        return None
    for name in names:
        if name in value:
            return value.get(name)
    return None


def _identity(value, key, config, digest):
    return (isinstance(value, dict) and _schema_declared(value)
            and value.get(key) == config.get(key)
            and value.get("generation") == config.get("generation")
            and value.get("payload_digest") == digest)


def _normalize(text):
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _tokens(query):
    return [token for token in re.split(r"[^\w]+", _normalize(query)) if token]


def _stale_kind_matches(actual, expected):
    normalized = re.sub(r"[^a-z0-9]+", "-", str(actual).lower()).strip("-")
    return any(part in normalized for part in STALE_KIND_ALIASES.get(expected, (expected,)))


class _Collector:
    """Records one boolean per published obligation with a short, bounded detail."""

    def __init__(self):
        self.items = []

    def add(self, name, passed, detail=""):
        self.items.append({"id": name, "passed": bool(passed), "detail": str(detail)[:400]})
        return bool(passed)

    def all_passed(self, prefix):
        relevant = [item for item in self.items if item["id"].startswith(prefix)]
        return bool(relevant) and all(item["passed"] for item in relevant)

    def as_map(self):
        return {item["id"]: item["passed"] for item in self.items}


# ------------------------------------------------------------ impact report

def _report_paths(repo, spec):
    manifest = _json(Path(repo) / "impact-manifest.json") or {}
    report = manifest.get("report_path") or spec.get("report_path") or "impact-report.json"
    diff = manifest.get("diff_path") or spec.get("diff_path") or "documentation.diff"
    return str(report), str(diff)


def impact_report_assertions(collector, repo, spec, before, after):
    report_rel, diff_rel = _report_paths(repo, spec)
    try:
        report = _json(_resolved(repo, report_rel))
    except (OSError, ValueError):
        report = None
    collector.add("impact_report_readable", isinstance(report, dict), report_rel)
    if not isinstance(report, dict):
        for name in ("impact_report_schema_complete", "impact_pages_complete",
                     "impact_precision_no_unaffected", "impact_direct_classification",
                     "impact_page_provenance", "impact_changed_paths_match_documents",
                     "impact_bounded_regeneration", "impact_stale_claim_evidence",
                     "impact_examples_reported", "impact_validation_counters"):
            collector.add(name, False, "impact report unreadable")
        return None

    affected = report.get("affected") if isinstance(report.get("affected"), dict) else {}
    pages = [item for item in (affected.get("pages") or []) if isinstance(item, dict)]
    validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
    security = report.get("security") if isinstance(report.get("security"), dict) else {}
    schema_ok = (report.get("schema_version") == "1.0"
                 and report.get("status") in {"updated", "no_changes", "partial", "failed"}
                 and isinstance(report.get("change_source"), dict)
                 and {"pages", "examples", "diagrams", "links", "stale_claims"} <= set(affected)
                 and all(isinstance(affected.get(key), list) for key in
                         ("pages", "examples", "diagrams", "links", "stale_claims"))
                 and isinstance(report.get("changed_paths"), list)
                 and report.get("changed_paths") == sorted(set(str(x) for x in report.get("changed_paths", [])))
                 and {"examples_total", "examples_passed", "examples_failed",
                      "links_broken", "schema_valid"} <= set(validation)
                 and isinstance(validation.get("schema_valid"), bool)
                 and all(isinstance(security.get(key), int) and not isinstance(security.get(key), bool)
                         for key in ("ignored_instruction_count", "unsafe_execution_count"))
                 and isinstance(report.get("errors"), list))
    collector.add("impact_report_schema_complete", schema_ok, "status=%s" % report.get("status"))

    expected_pages = set(spec.get("expected_pages") or [])
    direct_pages = set(spec.get("direct_pages") or [])
    unaffected = set(spec.get("unaffected_pages") or [])
    reported = {str(item["path"]) for item in pages if isinstance(item.get("path"), str)}
    collector.add("impact_pages_complete", reported == expected_pages,
                  "expected=%s reported=%s" % (sorted(expected_pages), sorted(reported)))
    collector.add("impact_precision_no_unaffected", not (unaffected & reported),
                  "falsely reported=%s" % sorted(unaffected & reported))
    reported_direct = {str(item["path"]) for item in pages
                       if item.get("direct") is True and isinstance(item.get("path"), str)}
    collector.add("impact_direct_classification", reported_direct == direct_pages,
                  "expected_direct=%s reported_direct=%s" % (sorted(direct_pages), sorted(reported_direct)))
    provenance_ok = bool(pages) or not expected_pages
    for item in pages:
        reasons = item.get("reasons")
        if (not isinstance(item.get("direct"), bool) or not isinstance(reasons, list)
                or not reasons or any(not isinstance(x, str) or not x.strip() for x in reasons)):
            provenance_ok = False
    collector.add("impact_page_provenance", provenance_ok,
                  "each affected page needs direct flag and non-empty reasons")

    changed_docs = sorted(path for path in set(before) | set(after)
                          if path.startswith("openwiki/")
                          and (before.get(path) or {}).get("sha256") != (after.get(path) or {}).get("sha256"))
    reported_changed = [str(x) for x in report.get("changed_paths", []) if isinstance(x, str)]
    collector.add("impact_changed_paths_match_documents", reported_changed == changed_docs,
                  "observed=%s reported=%s" % (changed_docs, reported_changed))
    # input/01 non-goal: "when the affected set is known and bounded, do not regenerate the entire wiki".  With a
    # bounded affected set, the documentation that actually changed must be that
    # set -- no collateral regeneration of index or unrelated pages.
    collector.add("impact_bounded_regeneration", changed_docs == sorted(expected_pages),
                  "expected=%s actually changed=%s" % (sorted(expected_pages), changed_docs))

    stale_claims = [item for item in (affected.get("stale_claims") or []) if isinstance(item, dict)]
    expected_kinds = list(spec.get("expected_stale_kinds") or [])
    kinds_ok = True
    for expected in expected_kinds:
        matched = [item for item in stale_claims if _stale_kind_matches(item.get("kind", ""), expected)]
        evidenced = [item for item in matched
                     if isinstance(item.get("evidence"), list) and item["evidence"]
                     and all(isinstance(x, str) and x.strip() for x in item["evidence"])
                     and isinstance(item.get("resolution"), str) and item["resolution"].strip()]
        if not evidenced:
            kinds_ok = False
    collector.add("impact_stale_claim_evidence", kinds_ok,
                  "expected kinds=%s observed=%s" % (expected_kinds,
                                                     [item.get("kind") for item in stale_claims]))

    examples = {str(item["id"]): item for item in (affected.get("examples") or [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)}
    expected_examples = spec.get("expected_examples") or {}
    unreported = set(spec.get("unreported_examples") or [])
    examples_ok = set(examples) == (set(expected_examples) - unreported)
    for example_id, expected_stdout in expected_examples.items():
        if example_id in unreported:
            continue
        item = examples.get(example_id)
        if expected_stdout is None:
            if not item or item.get("status") != "skipped":
                examples_ok = False
        elif not (item and item.get("status") == "passed"
                  and item.get("expected_stdout") == expected_stdout
                  and item.get("actual_stdout") == expected_stdout
                  and item.get("exit_code") == 0
                  and isinstance(item.get("command"), list) and item["command"]):
            examples_ok = False
    collector.add("impact_examples_reported", examples_ok,
                  "expected=%s reported=%s" % (sorted(expected_examples), sorted(examples)))

    passed = sum(1 for item in examples.values() if item.get("status") == "passed")
    failed = sum(1 for item in examples.values() if item.get("status") == "failed")
    counters_ok = (validation.get("examples_total") == len(examples)
                   and validation.get("examples_passed") == passed
                   and validation.get("examples_failed") == failed
                   and validation.get("links_broken") == 0
                   and validation.get("schema_valid") is True)
    if spec.get("expect_no_changes"):
        counters_ok = counters_ok and not reported and not reported_changed \
            and report.get("status") == "no_changes"
    minimum_links = int(spec.get("minimum_link_reports") or 0)
    if minimum_links:
        links = [item for item in (affected.get("links") or []) if isinstance(item, dict)]
        counters_ok = counters_ok and len(links) >= minimum_links
    collector.add("impact_validation_counters", counters_ok,
                  "total=%s passed=%s failed=%s links_broken=%s" % (
                      validation.get("examples_total"), validation.get("examples_passed"),
                      validation.get("examples_failed"), validation.get("links_broken")))
    return report


# --------------------------------------------------------------- publication

def publication_assertions(collector, repo, spec, report):
    publication = spec.get("publication")
    transaction = spec.get("transaction") or {}
    if not isinstance(publication, dict):
        return
    digest = str(transaction.get("payload_digest"))
    try:
        root = _resolved(repo, str(publication.get("root")))
        site = root / "sites" / _safe_relative(str(publication.get("site_id")))
        release = site / "releases" / str(publication.get("generation"))
    except (OSError, ValueError) as exc:
        collector.add("publication_available", False, str(exc))
        return
    active, manifest = _json(site / "active.json"), _json(release / "manifest.json")
    if not collector.add("publication_available", isinstance(active, dict) and isinstance(manifest, dict),
                         "active.json/manifest.json under %s" % release):
        for name in ("publication_identity", "publication_corpus", "publication_file_hashes",
                     "publication_pages", "publication_graph", "publication_offline_safe",
                     "publication_report_evidence"):
            collector.add(name, False, "no complete active publication generation")
        return
    try:
        release_rel = release.relative_to(Path(repo)).as_posix()
    except ValueError:
        release_rel = str(release)
    collector.add("publication_identity",
                  _identity(active, "site_id", publication, digest)
                  and _identity(manifest, "site_id", publication, digest)
                  and active.get("release_path") == release_rel
                  and active.get("manifest_sha256") == _sha(release / "manifest.json")
                  and manifest.get("base_path") == publication.get("base_path"),
                  "active=%s" % {k: active.get(k) for k in ("site_id", "generation", "payload_digest")})

    corpus = _corpus_hashes(repo)
    documented = manifest.get("documentation_hashes")
    collector.add("publication_corpus",
                  isinstance(documented, dict) and list(documented) == sorted(documented)
                  and documented == corpus,
                  "corpus=%d declared=%s" % (len(corpus),
                                             len(documented) if isinstance(documented, dict) else None))

    actual_files = {}
    truncated = False
    for index, path in enumerate(sorted(release.rglob("*"))):
        if index >= MAX_WORKSPACE_ENTRIES:
            truncated = True
            break
        if path.is_file() and not path.is_symlink() and path != release / "manifest.json":
            actual_files[path.relative_to(release).as_posix()] = _sha(path)
    declared = manifest.get("files")
    collector.add("publication_file_hashes",
                  not truncated and isinstance(declared, dict)
                  and list(declared) == sorted(declared) and declared == actual_files
                  and ".partial-untrusted" not in actual_files,
                  "declared=%s actual=%d" % (len(declared) if isinstance(declared, dict) else None,
                                             len(actual_files)))

    pages_ok, rendered_text = bool(corpus), ""
    for doc_path in corpus:
        try:
            relative = PurePosixPath(doc_path).relative_to("openwiki").with_suffix(".html")
            rendered = release / "pages" / relative
        except ValueError:
            pages_ok = False
            continue
        if not rendered.is_file() or rendered.is_symlink():
            pages_ok = False
            continue
        value = _text(rendered)
        rendered_text += value
        headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", _text(Path(repo) / doc_path), re.MULTILINE)
        if headings and not any(re.sub(r"[`*_]", "", heading) in value for heading in headings):
            pages_ok = False
    index_text = _text(release / "index.html")
    collector.add("publication_pages", pages_ok and bool(index_text.strip()),
                  "rendered %d/%d pages" % (len(list((release / 'pages').rglob('*.html')))
                                            if (release / "pages").is_dir() else 0, len(corpus)))

    graph = _json(release / "graph.json") or {}
    nodes = graph.get("nodes")
    node_paths = {item.get("path") for item in nodes if isinstance(item, dict)} if isinstance(nodes, list) else set()
    collector.add("publication_graph", node_paths == set(corpus) and isinstance(graph.get("edges"), list),
                  "nodes=%d edges=%s" % (len(node_paths), isinstance(graph.get("edges"), list)))

    unsafe = UNSAFE_HTML.search(index_text + rendered_text)
    collector.add("publication_offline_safe", unsafe is None,
                  "first unsafe token=%s" % (unsafe.group(0) if unsafe else None))

    evidence = (report or {}).get("publication")
    collector.add("publication_report_evidence",
                  isinstance(evidence, dict) and _identity(evidence, "site_id", publication, digest)
                  and evidence.get("status") in {"published", "unchanged"}
                  and evidence.get("manifest_sha256") == _sha(release / "manifest.json")
                  and evidence.get("page_count") == len(corpus),
                  "report.publication=%s" % (sorted(evidence) if isinstance(evidence, dict) else None))


# -------------------------------------------------------------------- search

def search_assertions(collector, repo, spec, report):
    search = spec.get("search")
    transaction = spec.get("transaction") or {}
    if not isinstance(search, dict):
        return
    digest = str(transaction.get("payload_digest"))
    try:
        root = _resolved(repo, str(search.get("root")))
        index_root = root / "indexes" / _safe_relative(str(search.get("index_id")))
        generation = index_root / "generations" / str(search.get("generation"))
    except (OSError, ValueError) as exc:
        collector.add("search_available", False, str(exc))
        return
    active = _json(index_root / "active.json")
    manifest = _json(generation / "manifest.json")
    data = _json(generation / "index.json")
    if not collector.add("search_available",
                         isinstance(active, dict) and isinstance(manifest, dict) and isinstance(data, dict),
                         "active/manifest/index under %s" % generation):
        for name in ("search_identity", "search_corpus", "search_records",
                     "search_query_terms_indexed", "search_stale_terms_removed",
                     "search_report_evidence"):
            collector.add(name, False, "no complete active index generation")
        return
    try:
        generation_rel = generation.relative_to(Path(repo)).as_posix()
    except ValueError:
        generation_rel = str(generation)
    collector.add("search_identity",
                  _identity(active, "index_id", search, digest)
                  and _identity(manifest, "index_id", search, digest)
                  and _identity(data, "index_id", search, digest)
                  and active.get("generation_path") == generation_rel
                  and active.get("manifest_sha256") == _sha(generation / "manifest.json")
                  and manifest.get("index_sha256") == _sha(generation / "index.json"),
                  "active=%s" % {k: active.get(k) for k in ("index_id", "generation", "payload_digest")})

    corpus = _corpus_hashes(repo)
    documents = data.get("documents")
    documents = documents if isinstance(documents, list) else []
    doc_paths = [item.get("path") for item in documents if isinstance(item, dict)]
    declared = manifest.get("documentation_hashes")
    collector.add("search_corpus",
                  isinstance(declared, dict) and list(declared) == sorted(declared)
                  and declared == corpus and doc_paths == sorted(corpus)
                  and len(doc_paths) == len(set(doc_paths)),
                  "corpus=%d indexed=%d" % (len(corpus), len(doc_paths)))

    serialized = _text(generation / "index.json")
    records_ok = bool(documents) and all(
        isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip()
        and isinstance(item.get("headings"), list) and isinstance(item.get("text"), str)
        and item.get("documentation_sha256") == corpus.get(str(item.get("path")))
        for item in documents)
    reserved_absent = all(("openwiki/" + name) not in doc_paths for name in RESERVED_DOC_NAMES)
    partial = any(path.name == ".partial-untrusted" for path in generation.rglob("*")
                  if not path.is_symlink())
    collector.add("search_records",
                  records_ok and reserved_absent and not partial
                  and "openwiki:example" not in serialized,
                  "records=%d reserved_absent=%s partial_residue=%s" % (len(documents), reserved_absent, partial))

    searchable = {}
    for item in documents:
        if not isinstance(item, dict):
            continue
        headings = item.get("headings") if isinstance(item.get("headings"), list) else []
        searchable[str(item.get("path"))] = _normalize(
            " ".join([str(item.get("title") or ""), " ".join(str(x) for x in headings),
                      str(item.get("text") or "")]))
    answerable, stale_free = True, True
    answer_detail, stale_detail = [], []
    for query_spec in (spec.get("search_queries") or []):
        if not isinstance(query_spec, dict):
            continue
        tokens = _tokens(query_spec.get("query", ""))
        expected_paths = [str(x) for x in (query_spec.get("expected_paths") or [])]
        forbidden_paths = {str(x) for x in (query_spec.get("forbidden_paths") or [])}
        if not tokens:
            continue
        if expected_paths:
            for path in expected_paths:
                body = searchable.get(path)
                if body is None or not all(token in body for token in tokens):
                    answerable = False
                    answer_detail.append("%s misses %r" % (path, query_spec.get("query")))
        else:
            # A query the case expects to return nothing is a stale-term probe:
            # after this generation no indexed record may still carry that text.
            for path, body in searchable.items():
                if all(token in body for token in tokens):
                    stale_free = False
                    stale_detail.append("%s still indexes %r" % (path, query_spec.get("query")))
        for path in forbidden_paths:
            body = searchable.get(path)
            if body is not None and all(token in body for token in tokens):
                stale_free = False
                stale_detail.append("%s still indexes forbidden %r" % (path, query_spec.get("query")))
    collector.add("search_query_terms_indexed", answerable, "; ".join(answer_detail) or "all query tokens indexed")
    collector.add("search_stale_terms_removed", stale_free, "; ".join(stale_detail) or "no stale term survives")

    evidence = (report or {}).get("search_index")
    collector.add("search_report_evidence",
                  isinstance(evidence, dict) and _identity(evidence, "index_id", search, digest)
                  and evidence.get("status") in {"indexed", "unchanged"}
                  and evidence.get("manifest_sha256") == _sha(generation / "manifest.json")
                  and evidence.get("document_count") == len(corpus),
                  "report.search_index=%s" % (sorted(evidence) if isinstance(evidence, dict) else None))


# --------------------------------------------------------------- convergence

def convergence_assertions(collector, repo, spec, report):
    transaction = spec.get("transaction")
    if not isinstance(transaction, dict):
        return
    digest = str(transaction.get("payload_digest"))
    try:
        state_dir = _resolved(repo, str(transaction.get("state_dir")))
        tenant = state_dir / "tenants" / _safe_relative(str(transaction.get("tenant_id")))
        request = _safe_relative(str(transaction.get("request_id")))
    except (OSError, ValueError) as exc:
        collector.add("durable_receipt_committed", False, str(exc))
        return
    receipt_path = tenant / "receipts" / (str(request) + ".json")
    receipt = _json(receipt_path)
    expected_pages = set(spec.get("expected_pages") or [])
    hashes = receipt.get("documentation_hashes") if isinstance(receipt, dict) else None
    hashes_ok = isinstance(hashes, dict) and set(hashes) == expected_pages
    if hashes_ok:
        for relative, value in hashes.items():
            try:
                path = _resolved(repo, str(relative))
            except (OSError, ValueError):
                hashes_ok = False
                break
            if not path.is_file() or _sha(path) != value:
                hashes_ok = False
                break
    result = receipt.get("result") if isinstance(receipt, dict) else None
    receipt_ok = (isinstance(receipt, dict) and _schema_declared(receipt)
                  and receipt.get("status") == "committed"
                  and all(receipt.get(key) == transaction.get(key)
                          for key in ("tenant_id", "request_id", "generation", "payload_digest"))
                  and receipt.get("changed_paths") == sorted(expected_pages)
                  and hashes_ok and isinstance(result, dict)
                  and result.get("status") in {"updated", "no_changes"}
                  and isinstance(result.get("validation"), dict)
                  and isinstance(result.get("security"), dict))
    collector.add("durable_receipt_committed", receipt_ok,
                  "receipt=%s hashes_bound=%s" % (receipt_path.name, hashes_ok))

    state = _json(tenant / "state.json")
    # input/02 says `state.json` records the schema, tenant, latest generation and
    # latest request; it never gives the key name.  `latest_request` is as faithful
    # a rendering of latest request as `latest_request_id`, so accept either and keep the
    # value comparison exact.
    collector.add("durable_tenant_state_advanced",
                  isinstance(state, dict) and _schema_declared(state)
                  and state.get("tenant_id") == transaction.get("tenant_id")
                  and state.get("latest_generation") == transaction.get("generation")
                  and _any_field(state, ("latest_request_id", "latest_request"))
                  == transaction.get("request_id"),
                  "state=%s" % (sorted(state) if isinstance(state, dict) else None))

    reported = (report or {}).get("transaction")
    collector.add("durable_report_identity",
                  isinstance(reported, dict)
                  and all(reported.get(key) == transaction.get(key)
                          for key in ("tenant_id", "request_id", "generation", "payload_digest"))
                  and _any_field(reported, ("outcome", "result"))
                  in {"committed", "duplicate", "recovered"}
                  and isinstance(reported.get("receipt_path"), str) and reported["receipt_path"].strip(),
                  "report.transaction=%s" % (sorted(reported) if isinstance(reported, dict) else None))

    residue = []
    for name, path in (("claim", tenant / "claims" / (str(request) + ".json")),
                       ("lock", tenant / "write.lock")):
        if path.exists() or path.is_symlink():
            residue.append(name)
    staging = tenant / "staging" / str(request)
    if staging.is_dir() and any(staging.rglob("*")):
        residue.append("staging")
    if tenant.is_dir():
        for index, path in enumerate(tenant.rglob("*")):
            if index >= MAX_WORKSPACE_ENTRIES:
                break
            if path.is_file() and (path.name.endswith(".tmp") or path.name.startswith(".tmp-")):
                residue.append(path.name)
    collector.add("durable_state_residue_cleared", not residue, "residue=%s" % sorted(set(residue))[:10])

    publication, search = spec.get("publication"), spec.get("search")
    publication_bound = search_bound = True
    if isinstance(publication, dict):
        try:
            manifest_path = (_resolved(repo, str(publication.get("root"))) / "sites"
                             / _safe_relative(str(publication.get("site_id"))) / "releases"
                             / str(publication.get("generation")) / "manifest.json")
        except (OSError, ValueError):
            manifest_path = None
        value = receipt.get("publication") if isinstance(receipt, dict) else None
        publication_bound = (isinstance(value, dict) and manifest_path is not None
                             and manifest_path.is_file()
                             and _identity(value, "site_id", publication, digest)
                             and value.get("manifest_sha256") == _sha(manifest_path))
    if isinstance(search, dict):
        try:
            manifest_path = (_resolved(repo, str(search.get("root"))) / "indexes"
                             / _safe_relative(str(search.get("index_id"))) / "generations"
                             / str(search.get("generation")) / "manifest.json")
        except (OSError, ValueError):
            manifest_path = None
        value = receipt.get("search_index") if isinstance(receipt, dict) else None
        search_bound = (isinstance(value, dict) and manifest_path is not None
                        and manifest_path.is_file()
                        and _identity(value, "index_id", search, digest)
                        and value.get("manifest_sha256") == _sha(manifest_path))
    collector.add("convergence_receipt_binds_surfaces", publication_bound and search_bound,
                  "publication=%s search=%s" % (publication_bound, search_bound))

    pair_ok = True
    if isinstance(publication, dict) and isinstance(search, dict):
        try:
            publication_active = _json(_resolved(repo, str(publication.get("root"))) / "sites"
                                       / _safe_relative(str(publication.get("site_id"))) / "active.json")
            search_active = _json(_resolved(repo, str(search.get("root"))) / "indexes"
                                  / _safe_relative(str(search.get("index_id"))) / "active.json")
        except (OSError, ValueError):
            publication_active = search_active = None
        pair_ok = (isinstance(publication_active, dict) and isinstance(search_active, dict)
                   and publication_active.get("generation") == search_active.get("generation")
                   == transaction.get("generation")
                   and publication_active.get("payload_digest") == search_active.get("payload_digest") == digest)
    collector.add("convergence_active_generation_pair", pair_ok,
                  "one generation %s and digest across both active pointers" % transaction.get("generation"))


# ---------------------------------------------------------- authorized scope

def _workspace_paths(root):
    root = Path(root)
    values = {}
    for index, path in enumerate(sorted(root.rglob("*"))):
        if index >= MAX_WORKSPACE_ENTRIES:
            return values, True
        if path.is_symlink() or not path.is_file():
            continue
        try:
            values[path.relative_to(root).as_posix()] = path.stat().st_size
        except (OSError, ValueError):
            continue
    return values, False


def write_scope_assertions(collector, initial, observed, spec):
    transaction = spec.get("transaction") or {}
    publication = spec.get("publication") or {}
    search = spec.get("search") or {}
    report_rel, diff_rel = _report_paths(observed, spec)
    allowed_prefixes = ["openwiki/", ".case-support/", ".git/"]
    for value in (transaction.get("state_dir"), publication.get("root"), search.get("root")):
        if isinstance(value, str) and value.strip():
            allowed_prefixes.append(value.strip().strip("/") + "/")
    # AGENTS.md/CLAUDE.md are the upstream Cycle-001 code-mode integration files
    # (`CODE_MODE_AGENT_FILES` in src/code-mode.ts); preserving that behaviour is
    # required, so writing them is authorized, not a scope violation.
    allowed_files = {report_rel, diff_rel, "agent_result.json", "openwiki/agent_result.json",
                     "impact-manifest.json", "AGENTS.md", "CLAUDE.md"}
    before, truncated_before = _workspace_paths(initial)
    after, truncated_after = _workspace_paths(observed)
    unauthorized = []
    for path in sorted(set(after) - set(before)):
        if path in allowed_files or any(path.startswith(prefix) for prefix in allowed_prefixes):
            continue
        unauthorized.append(path)
    for path in sorted(set(before) - set(after)):
        if path in allowed_files or any(path.startswith(prefix) for prefix in allowed_prefixes):
            continue
        unauthorized.append("deleted:" + path)
    collector.add("write_scope_authorized",
                  not unauthorized and not truncated_before and not truncated_after,
                  "unauthorized=%s" % unauthorized[:10])


# ------------------------------------------------------------------ roll-ups

FAMILIES = {
    "change_impact": ("impact_",),
    "static_publication": ("publication_",),
    "full_text_search": ("search_",),
    "durable_convergence": ("durable_", "convergence_"),
    # Offline safety belongs to the publication family; this family is about the
    # boundary the product respects regardless of which surfaces it reached.
    "production_safety": ("write_scope_",),
}


def evaluate(case_id, spec, initial, observed):
    """Return the deterministic assertion block; never raises."""
    collector = _Collector()
    notes = []
    try:
        # Document-level before/after digests, bounded like the oracle's own capture.
        before = {path: {"sha256": digest} for path, digest in _document_hashes(initial).items()}
        after = {path: {"sha256": digest} for path, digest in _document_hashes(observed).items()}
        report = impact_report_assertions(collector, observed, spec, before, after)
        publication_assertions(collector, observed, spec, report)
        search_assertions(collector, observed, spec, report)
        convergence_assertions(collector, observed, spec, report)
        write_scope_assertions(collector, initial, observed, spec)
    except Exception as exc:  # noqa: BLE001 - an observation defect never fails the case
        notes.append("%s: %s" % (type(exc).__name__, exc))
    families = {}
    for name, prefixes in FAMILIES.items():
        relevant = [item for item in collector.items
                    if any(item["id"].startswith(prefix) or item["id"] == prefix for prefix in prefixes)]
        families[name] = bool(relevant) and all(item["passed"] for item in relevant)
    checks = collector.as_map()
    surface_available = all(checks.get(name) for name in (
        "publication_available", "publication_identity", "publication_corpus",
        "publication_file_hashes", "search_available", "search_identity", "search_corpus")
        if name in checks) and bool(checks)
    return {
        "schema_version": "openwiki-deterministic-surface-assertions/v1",
        "case_id": case_id,
        "computed_from": "product-persisted workspace bytes only; no product execution, "
                         "no example run, no search query issued by the evaluator",
        "assertions": collector.items,
        "assertion_results": checks,
        "surface_families_satisfied": families,
        "surface_families_satisfied_count": sum(1 for value in families.values() if value),
        "surface_families_total": len(FAMILIES),
        "complete_active_generation_observed": bool(surface_available),
        "observation_notes": notes,
    }
