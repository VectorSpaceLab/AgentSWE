from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dev_cases"))

from harness_support import (  # noqa: E402
    HarnessError,
    PreparedCandidate,
    evaluate_case,
    extract_examples,
    file_hashes,
    materialize_case,
    parse_impact_report,
    parse_patch_paths,
    prepare_candidate,
    safe_relative_path,
    stale_kind_matches,
    strip_owned_content,
    validate_frontmatter,
    validate_internal_links,
    validate_delivery,
)


class PathSafetyTests(unittest.TestCase):
    def test_rejects_escaping_paths(self) -> None:
        with self.assertRaises(HarnessError):
            safe_relative_path("../secret")
        with self.assertRaises(HarnessError):
            safe_relative_path("/absolute")

    def test_patch_paths_are_deduplicated(self) -> None:
        patch = (
            "diff --git a/src/impact.ts b/src/impact.ts\n"
            "--- a/src/impact.ts\n+++ b/src/impact.ts\n"
            "diff --git a/test/impact.test.ts b/test/impact.test.ts\n"
        )
        self.assertEqual(parse_patch_paths(patch), ["src/impact.ts", "test/impact.test.ts"])

    def test_rejects_workflow_patch(self) -> None:
        with self.assertRaises(HarnessError):
            parse_patch_paths("diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n")

    def test_exact_delivery_and_single_apply_semantics(self) -> None:
        patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 <!-- markdownlint-disable MD033 MD041 -->
+<!-- transaction benchmark control -->
 
 <div align="center">
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solution.patch").write_text(patch, encoding="utf-8")
            (candidate / "edit_report.json").write_text(json.dumps({
                "schema_version": "1.0",
                "feature_summary": "delivery control",
                "changed_paths": ["README.md"],
                "commands": [],
                "compatibility_notes": [],
                "limitations": [],
            }), encoding="utf-8")
            (candidate / "run_report.json").write_text(json.dumps({
                "schema_version": "1.0",
                "status": "success",
                "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
                "errors": [],
                "runtime_seconds": 0,
                "peak_memory_bytes": 0,
                "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0},
            }), encoding="utf-8")
            validate_delivery(candidate)
            prepared = prepare_candidate(ROOT, candidate, root / "work", install=False)
            self.assertEqual(prepared.patch_paths, ["README.md"])
            self.assertIn("transaction benchmark control", (prepared.source_dir / "README.md").read_text(encoding="utf-8"))
            (candidate / "extra.log").write_text("not allowed", encoding="utf-8")
            with self.assertRaises(HarnessError):
                validate_delivery(candidate)

    def test_legacy_nested_resource_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate"
            candidate.mkdir()
            (candidate / "solution.patch").write_text("diff --git a/README.md b/README.md\n", encoding="utf-8")
            (candidate / "edit_report.json").write_text(json.dumps({
                "schema_version": "1.0", "feature_summary": "legacy", "changed_paths": ["README.md"],
                "commands": [], "compatibility_notes": [], "limitations": []
            }), encoding="utf-8")
            (candidate / "run_report.json").write_text(json.dumps({
                "schema_version": "1.0", "status": "success", "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
                "errors": [], "runtime_seconds": 0, "peak_memory_mb": 1,
                "resource_usage": {"gateway": 0, "serper": 0, "web_retrieval": 0}
            }), encoding="utf-8")
            with self.assertRaises(HarnessError):
                validate_delivery(candidate)


class FixtureTests(unittest.TestCase):
    def case_dirs(self) -> list[Path]:
        return sorted((ROOT / "dev_cases").glob("dev_???")) + sorted((ROOT / "test_cases").glob("test_???"))

    def test_all_cases_materialize_with_two_commits_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for case_dir in self.case_dirs():
                spec = None
                if case_dir.name.startswith("test_"):
                    spec = json.loads((ROOT / "evaluator" / "manifests" / f"{case_dir.name}.json").read_text(encoding="utf-8"))
                repo, spec, before = materialize_case(case_dir, Path(temp) / case_dir.name, spec)
                self.assertEqual(spec["case_id"], case_dir.name)
                self.assertTrue((repo / "impact-manifest.json").is_file())
                self.assertTrue(before.is_dir())
                self.assertTrue(file_hashes(repo))

    def test_deleted_setup_file_is_materialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = ROOT / "test_cases" / "test_004"
            spec = json.loads((ROOT / "evaluator" / "manifests" / "test_004.json").read_text(encoding="utf-8"))
            repo, _, _ = materialize_case(case, Path(temp) / "test_004", spec)
            self.assertFalse((repo / "docs" / "setup.md").exists())
            self.assertTrue((repo / "docs" / "setup" / "install.md").is_file())
            issues = validate_internal_links(repo)
            self.assertTrue(any("setup.md#install" in issue for issue in issues))

    def test_fixture_frontmatter_and_example_metadata_are_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            example_count = 0
            for case_dir in self.case_dirs():
                spec = None
                if case_dir.name.startswith("test_"):
                    spec = json.loads((ROOT / "evaluator" / "manifests" / f"{case_dir.name}.json").read_text(encoding="utf-8"))
                repo, _, _ = materialize_case(case_dir, Path(temp) / case_dir.name, spec)
                for page in (repo / "openwiki").rglob("*.md"):
                    self.assertTrue(validate_frontmatter(page), page)
                example_count += len(extract_examples(repo))
            self.assertGreaterEqual(example_count, 11)


class ContractTests(unittest.TestCase):
    def fake_no_change_script(self, durable: bool) -> str:
        durable_body = """
if (manifest.transaction) {
  const tx = manifest.transaction;
  const tenant = `${tx.state_dir}/tenants/${tx.tenant_id}`;
  fs.mkdirSync(`${tenant}/receipts`, {recursive: true});
  const receipt = {
    schema_version: '1.0', tenant_id: tx.tenant_id, request_id: tx.request_id,
    generation: tx.generation, payload_digest: tx.payload_digest, status: 'committed',
    changed_paths: [], documentation_hashes: {},
    result: {status: 'no_changes', validation: report.validation, security: report.security},
    publication: report.publication, search_index: report.search_index
  };
  const receiptPath = `${tenant}/receipts/${tx.request_id}.json`;
  if (!fs.existsSync(receiptPath)) fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\\n');
  fs.writeFileSync(`${tenant}/state.json`, JSON.stringify({schema_version: '1.0', tenant_id: tx.tenant_id, latest_generation: tx.generation, latest_request_id: tx.request_id}, null, 2) + '\\n');
  report.transaction = {tenant_id: tx.tenant_id, request_id: tx.request_id, generation: tx.generation, payload_digest: tx.payload_digest, receipt_path: receiptPath, outcome: duplicate ? 'duplicate' : 'committed'};
}
""" if durable else ""
        return """#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const args = process.argv.slice(2);
if (args[0] === 'search') {
  const root=args[args.indexOf('--index-root')+1], id=args[args.indexOf('--index-id')+1], query=args[args.indexOf('--query')+1] || '', limit=Number(args[args.indexOf('--limit')+1] || 10);
  const active=JSON.parse(fs.readFileSync(path.join(root,'indexes',id,'active.json'),'utf8')); const data=JSON.parse(fs.readFileSync(path.join(root,'indexes',id,'generations',String(active.generation),'index.json'),'utf8')); const tokens=query.normalize('NFKC').toLocaleLowerCase().split(/\\s+/).filter(Boolean);
  const results=data.documents.filter(d=>tokens.every(t=>d.text.normalize('NFKC').toLocaleLowerCase().includes(t))).slice(0,limit).map((d,i)=>({path:d.path,title:d.title,anchor:'page',snippet:d.text.slice(0,120),score:1/(i+1)}));
  process.stdout.write(JSON.stringify({schema_version:'1.0',index_id:id,generation:active.generation,payload_digest:active.payload_digest,query,results})); process.exit(0);
}
const manifestPath = args[args.indexOf('--impact-manifest') + 1];
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const tx = manifest.transaction; const receiptPath = tx ? path.join(tx.state_dir,'tenants',tx.tenant_id,'receipts',tx.request_id+'.json') : ''; const duplicate = !!(tx && fs.existsSync(receiptPath));
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const walk = root => { let out=[]; if (!fs.existsSync(root)) return out; for (const ent of fs.readdirSync(root,{withFileTypes:true})) { const p=path.join(root,ent.name); if(ent.isDirectory()) out=out.concat(walk(p)); else if(ent.isFile() && p.endsWith('.md') && !['INSTRUCTIONS.md','_plan.md','log.md'].includes(ent.name)) out.push(p); } return out.sort(); };
const allFiles = root => { let out=[]; if (!fs.existsSync(root)) return out; for (const ent of fs.readdirSync(root,{withFileTypes:true})) { const p=path.join(root,ent.name); if(ent.isDirectory()) out=out.concat(allFiles(p)); else if(ent.isFile()) out.push(p); } return out.sort(); };
const docs = walk(path.join(process.cwd(),'openwiki'));
const docHashes = {}; for (const p of docs) docHashes[path.relative(process.cwd(),p).split(path.sep).join('/')] = sha(fs.readFileSync(p));
const report = {
  schema_version: '1.0', status: 'no_changes',
  change_source: {kind: manifest.change_source.kind},
  affected: {pages: [], examples: [], diagrams: [], links: [], stale_claims: []},
  changed_paths: [],
  validation: {examples_total: 0, examples_passed: 0, examples_failed: 0, links_broken: 0, schema_valid: true},
  security: {ignored_instruction_count: 1, unsafe_execution_count: 0}, errors: []
};
const p = manifest.publication;
if (p && !duplicate) {
  const site = path.join(p.root,'sites',p.site_id), release = path.join(site,'releases',String(p.generation));
  fs.mkdirSync(path.join(release,'pages'),{recursive:true});
  for (const [doc] of Object.entries(docHashes)) { const rel=doc.slice('openwiki/'.length).replace(/\.md$/,'.html'); const body=fs.readFileSync(path.join(process.cwd(),doc),'utf8').replace(/</g,'&lt;').replace(/>/g,'&gt;'); const dest=path.join(release,'pages',rel); fs.mkdirSync(path.dirname(dest),{recursive:true}); fs.writeFileSync(dest,`<!doctype html><html><body><pre>${body}</pre></body></html>${String.fromCharCode(10)}`); }
  fs.writeFileSync(path.join(release,'index.html'),'<!doctype html><html><body>OpenWiki offline index</body></html>'+String.fromCharCode(10));
  fs.writeFileSync(path.join(release,'graph.json'),JSON.stringify({schema_version:'1.0',site_id:p.site_id,generation:p.generation,payload_digest:manifest.transaction.payload_digest,nodes:Object.keys(docHashes).map(path=>({path})),edges:[]},null,2)+String.fromCharCode(10));
  const files={}; for(const q of allFiles(release)) if(path.basename(q)!=='manifest.json') files[path.relative(release,q).split(path.sep).join('/')] = sha(fs.readFileSync(q));
  const pubManifest={schema_version:'1.0',site_id:p.site_id,generation:p.generation,payload_digest:manifest.transaction.payload_digest,base_path:p.base_path,documentation_hashes:docHashes,files};
  fs.writeFileSync(path.join(release,'manifest.json'),JSON.stringify(pubManifest,null,2)+String.fromCharCode(10));
  const active={schema_version:'1.0',site_id:p.site_id,generation:p.generation,payload_digest:manifest.transaction.payload_digest,release_path:path.relative(process.cwd(),release).split(path.sep).join('/'),manifest_sha256:sha(fs.readFileSync(path.join(release,'manifest.json')))};
  fs.mkdirSync(site,{recursive:true}); fs.writeFileSync(path.join(site,'active.json'),JSON.stringify(active,null,2)+String.fromCharCode(10));
  report.publication={schema_version:'1.0',site_id:p.site_id,generation:p.generation,payload_digest:manifest.transaction.payload_digest,active_path:path.relative(process.cwd(),path.join(site,'active.json')).split(path.sep).join('/'),manifest_path:path.relative(process.cwd(),path.join(release,'manifest.json')).split(path.sep).join('/'),manifest_sha256:active.manifest_sha256,page_count:Object.keys(docHashes).length,status:'published'};
}
const s = manifest.search;
if (s && !duplicate) {
  const root=path.join(s.root,'indexes',s.index_id), generation=path.join(root,'generations',String(s.generation)); fs.mkdirSync(generation,{recursive:true});
  const documents=Object.keys(docHashes).map(doc=>({path:doc,title:path.basename(doc,'.md'),headings:[],text:fs.readFileSync(path.join(process.cwd(),doc),'utf8').replace(/<!--.*?-->/gs,'').replace(/[`*_#>-]/g,' '),documentation_sha256:docHashes[doc]}));
  const data={schema_version:'1.0',index_id:s.index_id,generation:s.generation,payload_digest:manifest.transaction.payload_digest,documents}; fs.writeFileSync(path.join(generation,'index.json'),JSON.stringify(data,null,2)+String.fromCharCode(10));
  const sm={schema_version:'1.0',index_id:s.index_id,generation:s.generation,payload_digest:manifest.transaction.payload_digest,documentation_hashes:docHashes,index_sha256:sha(fs.readFileSync(path.join(generation,'index.json')))}; fs.writeFileSync(path.join(generation,'manifest.json'),JSON.stringify(sm,null,2)+String.fromCharCode(10));
  const active={schema_version:'1.0',index_id:s.index_id,generation:s.generation,payload_digest:manifest.transaction.payload_digest,generation_path:path.relative(process.cwd(),generation).split(path.sep).join('/'),manifest_sha256:sha(fs.readFileSync(path.join(generation,'manifest.json')))}; fs.mkdirSync(root,{recursive:true}); fs.writeFileSync(path.join(root,'active.json'),JSON.stringify(active,null,2)+String.fromCharCode(10));
  report.search_index={schema_version:'1.0',index_id:s.index_id,generation:s.generation,payload_digest:manifest.transaction.payload_digest,active_path:path.relative(process.cwd(),path.join(root,'active.json')).split(path.sep).join('/'),manifest_path:path.relative(process.cwd(),path.join(generation,'manifest.json')).split(path.sep).join('/'),manifest_sha256:active.manifest_sha256,document_count:documents.length,status:'indexed'};
}
if (duplicate && p) { const site=path.join(p.root,'sites',p.site_id), release=path.join(site,'releases',String(p.generation)); const a=JSON.parse(fs.readFileSync(path.join(site,'active.json'))), m=JSON.parse(fs.readFileSync(path.join(release,'manifest.json'))); report.publication={schema_version:'1.0',site_id:p.site_id,generation:p.generation,payload_digest:a.payload_digest,active_path:path.relative(process.cwd(),path.join(site,'active.json')).split(path.sep).join('/'),manifest_path:path.relative(process.cwd(),path.join(release,'manifest.json')).split(path.sep).join('/'),manifest_sha256:a.manifest_sha256,page_count:Object.keys(m.documentation_hashes).length,status:'unchanged'}; }
if (duplicate && s) { const root=path.join(s.root,'indexes',s.index_id), generation=path.join(root,'generations',String(s.generation)); const a=JSON.parse(fs.readFileSync(path.join(root,'active.json'))), m=JSON.parse(fs.readFileSync(path.join(generation,'manifest.json'))); report.search_index={schema_version:'1.0',index_id:s.index_id,generation:s.generation,payload_digest:a.payload_digest,active_path:path.relative(process.cwd(),path.join(root,'active.json')).split(path.sep).join('/'),manifest_path:path.relative(process.cwd(),path.join(generation,'manifest.json')).split(path.sep).join('/'),manifest_sha256:a.manifest_sha256,document_count:Object.keys(m.documentation_hashes).length,status:'unchanged'}; }
""" + durable_body + """
fs.writeFileSync(manifest.report_path, JSON.stringify(report, null, 2) + String.fromCharCode(10));
fs.writeFileSync(manifest.diff_path, '');
"""

    def run_fake_no_change(self, durable: bool):
        script = self.fake_no_change_script(durable)
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        source = root / "fake-source" / "dist"
        source.mkdir(parents=True)
        (source / "cli.js").write_text(script, encoding="utf-8")
        prepared = PreparedCandidate(source.parent, [], [], {})
        spec = json.loads((ROOT / "evaluator" / "manifests" / "test_006.json").read_text(encoding="utf-8"))
        result = evaluate_case(prepared, ROOT / "test_cases" / "test_006", root / "case", spec_override=spec)
        return temp, result

    def test_end_to_end_durable_no_change_success_path_scores_100(self) -> None:
        temp, result = self.run_fake_no_change(True)
        self.addCleanup(temp.cleanup)
        self.assertTrue(result.valid)
        self.assertEqual(result.score, 100.0)
        self.assertFalse(result.availability_ceiling_applied)
        self.assertTrue(all(item.passed for item in result.assertions))

    def test_intentionally_incomplete_control_is_behavioral_and_capped(self) -> None:
        temp, result = self.run_fake_no_change(False)
        self.addCleanup(temp.cleanup)
        self.assertTrue(result.valid)
        self.assertGreater(result.score, 0.0)
        self.assertLessEqual(result.score, 25.0)
        self.assertTrue(result.availability_ceiling_applied)

    def test_all_hidden_scenarios_keep_incomplete_control_behavioral(self) -> None:
        script = self.fake_no_change_script(False)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "fake-source" / "dist"
            source.mkdir(parents=True)
            (source / "cli.js").write_text(script, encoding="utf-8")
            prepared = PreparedCandidate(source.parent, [], [], {})
            results = []
            for manifest_path in sorted((ROOT / "evaluator" / "manifests").glob("test_*.json")):
                spec = json.loads(manifest_path.read_text(encoding="utf-8"))
                result = evaluate_case(
                    prepared,
                    ROOT / "test_cases" / spec["case_id"],
                    root / spec["case_id"],
                    spec_override=spec,
                )
                results.append(result)
            self.assertEqual(len(results), 6)
            self.assertTrue(all(result.valid for result in results))
            self.assertTrue(all(0 < result.score <= 25 for result in results))
            self.assertLessEqual(sum(result.score for result in results) / 6, 25.0)

    def test_complete_evaluator_control_scores_100_on_all_six_cases(self) -> None:
        """A complete evaluator-owned control exercises every scenario branch."""
        script = self.fake_no_change_script(True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "complete-control" / "dist"
            source.mkdir(parents=True)
            (source / "cli.js").write_text(script, encoding="utf-8")
            prepared = PreparedCandidate(source.parent, [], [], {})
            results = []
            for manifest_path in sorted((ROOT / "evaluator" / "manifests").glob("test_*.json")):
                spec = json.loads(manifest_path.read_text(encoding="utf-8"))
                spec.update({
                    "scenario": "commit_duplicate",
                    "preseed": None,
                    "preseed_surfaces": None,
                    "post_commit_corrupt_search": False,
                    "expected_pages": [],
                    "direct_pages": [],
                    "required_text": {},
                    "forbidden_text": {},
                    "expected_examples": {},
                    "expected_stale_kinds": [],
                    "preserved_tokens": [],
                    "preserved_anchors": [],
                    "search_queries": [{"query": "OpenWiki", "limit": 3, "expected_paths": []}],
                })
                result = evaluate_case(
                    prepared,
                    ROOT / "test_cases" / "test_006",
                    root / spec["case_id"],
                    spec_override=spec,
                )
                results.append(result)
            self.assertEqual(len(results), 6)
            self.assertTrue(all(result.valid for result in results))
            self.assertEqual([result.score for result in results], [100.0] * 6)

    def test_cycle001_equivalent_control_is_valid_but_cannot_dominate(self) -> None:
        script = self.fake_no_change_script(True)
        script = script.replace("const p = manifest.publication;", "const p = undefined;")
        script = script.replace("const s = manifest.search;", "const s = undefined;")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cycle001-control" / "dist"
            source.mkdir(parents=True)
            (source / "cli.js").write_text(script, encoding="utf-8")
            prepared = PreparedCandidate(source.parent, [], [], {})
            scores = []
            for manifest_path in sorted((ROOT / "evaluator" / "manifests").glob("test_*.json")):
                spec = json.loads(manifest_path.read_text(encoding="utf-8"))
                result = evaluate_case(
                    prepared,
                    ROOT / "test_cases" / spec["case_id"],
                    root / ("cycle001-" + spec["case_id"]),
                    spec_override=spec,
                )
                self.assertTrue(result.valid)
                scores.append(result.score)
            self.assertTrue(all(0.0 < score <= 20.0 for score in scores), scores)
            self.assertTrue(10.0 <= sum(scores) / len(scores) <= 20.0, scores)

    def test_pristine_entry_only_control_is_valid_behavioral_low_evidence(self) -> None:
        script = self.fake_no_change_script(False)
        script = script.replace("const p = manifest.publication;", "const p = undefined;")
        script = script.replace("const s = manifest.search;", "const s = undefined;")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "entry-only" / "dist"
            source.mkdir(parents=True)
            (source / "cli.js").write_text(script, encoding="utf-8")
            prepared = PreparedCandidate(source.parent, [], [], {})
            spec = json.loads((ROOT / "evaluator/manifests/test_006.json").read_text(encoding="utf-8"))
            result = evaluate_case(prepared, ROOT / "test_cases" / "test_006", root / "case", spec_override=spec)
            self.assertTrue(result.valid)
            self.assertGreater(result.score, 0.0)
            self.assertLessEqual(result.score, 20.0)

    def test_report_schema_accepts_complete_report(self) -> None:
        report = {
            "schema_version": "1.0",
            "status": "no_changes",
            "change_source": {"kind": "git"},
            "affected": {"pages": [], "examples": [], "diagrams": [], "links": [], "stale_claims": []},
            "changed_paths": [],
            "validation": {
                "examples_total": 0,
                "examples_passed": 0,
                "examples_failed": 0,
                "links_broken": 0,
                "schema_valid": True,
            },
            "security": {"ignored_instruction_count": 0, "unsafe_execution_count": 0},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "impact-report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            parsed, errors = parse_impact_report(path)
            self.assertEqual(parsed, report)
            self.assertEqual(errors, [])

    def test_owned_content_normalization_preserves_handwritten_signal(self) -> None:
        before = "---\ntype: A\n---\nNOTE\n<!-- openwiki:generated:start id=\"x\" -->old<!-- openwiki:generated:end id=\"x\" -->\n"
        after = "---\ntype: B\n---\nNOTE\n<!-- openwiki:generated:start id=\"x\" -->new<!-- openwiki:generated:end id=\"x\" -->\n"
        self.assertEqual(strip_owned_content(before), strip_owned_content(after))
        self.assertNotEqual(strip_owned_content(before), strip_owned_content(after.replace("NOTE", "CHANGED")))

    def test_stale_kind_synonyms(self) -> None:
        self.assertTrue(stale_kind_matches("mermaid-edge-stale", "diagram-edge"))
        self.assertTrue(stale_kind_matches("public-api-removal", "removed-api"))
        self.assertTrue(stale_kind_matches("compat-re-export", "compatibility-alias"))

    def test_rubric_weights_total_100(self) -> None:
        rubric = (ROOT / "evaluator" / "rubric.md").read_text(encoding="utf-8")
        weights = [int(value) for value in __import__("re").findall(r"^## \d+\..*?: (\d+) points$", rubric, __import__("re").MULTILINE)]
        self.assertEqual(weights, [15, 30, 30, 20, 5])
        self.assertEqual(sum(weights), 100)

    def test_counts_manifests_and_case_ownership(self) -> None:
        self.assertEqual(len(list((ROOT / "dev_cases").glob("dev_???"))), 2)
        self.assertEqual(len(list((ROOT / "test_cases").glob("test_???"))), 6)
        manifests = sorted((ROOT / "evaluator" / "manifests").glob("test_???.json"))
        self.assertEqual(len(manifests), 6)
        for path in manifests:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sum(value["weights"].values()), 100, path)
        self.assertFalse(list((ROOT / "test_cases").glob("test_*/**/case.json")))

    def test_builder_visible_material_has_no_hidden_case_answers(self) -> None:
        hidden_tokens = ["format-team", "identity-team", "reader-red", "runtime-team", "monorepo-team", "security-a", "legacyFetch", "alpha-v2", "runtime-012"]
        visible = []
        for root in (ROOT / "input", ROOT / "dev_cases"):
            for path in root.rglob("*"):
                if path.is_file() and path.suffix in {".md", ".json", ".py", ".ts", ".tsx", ".js"}:
                    visible.append(path.read_text(encoding="utf-8", errors="ignore"))
        joined = "\n".join(visible)
        for token in hidden_tokens:
            self.assertNotIn(token, joined)


if __name__ == "__main__":
    unittest.main()
