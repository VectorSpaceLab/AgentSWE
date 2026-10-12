"""The Optimization row cache is keyed by the evaluation configuration (stdlib unittest; Harbor and the native
controller are faked, no docker).

The cache under <home>/runs/optimization/row_cache is shared by every run in a home. Its key held the candidate, the
case, the benchmark tree and the evaluator source, so a run with another RUNTIME model, effort or provider reused the
starter rows another configuration had scored. The key now also holds the evaluation configuration: a run with the
same configuration still reuses the starter rows (as the paper's Lite runs did), a run with another one scores them
again, entries written under the old key are not reused, and the stored row is unchanged."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
OPT = ROOT / "runners" / "optimization"
TAU3 = "tau3-tool-agent-optimization-v1"
OSWORLD = "osworld-desktop-agent-optimization-v1"
KEY = "sk-test-row-cache-0000"
CONFIG_A = {"AGENTSWE_RUNTIME_RESPONSES_URL": "https://provider-a.example/v1/responses",
            "AGENTSWE_RUNTIME_MODEL": "model-a", "AGENTSWE_RUNTIME_EFFORT": "high", "AGENTSWE_JUDGE_EFFORT": "medium"}
RECORD_FIELDS = {"schema_version", "cache_key", "candidate_digest", "case_id", "benchmark_digest",
                 "protocol_source_digest", "created_at", "case", "eval_result"}


def load_adapter():
    with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AGENTSWE_HOME": home}):
        spec = importlib.util.spec_from_file_location("rc_optimization_adapter", OPT / "adapter.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class RowCacheConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bench = self.root / "bench"
        contract = {"schema_version": "3.0", "splits": {
            "dev": {"cases": [{"case_id": "dev_001"}, {"case_id": "dev_002"}]},
            "hidden": {"cases": [{"case_id": "test_001"}]}}}
        self.bench.mkdir()
        (self.bench / "task_contract.json").write_text(json.dumps(contract))
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        (self.candidate / "run_harness.py").write_text("print('starter')\n")
        self.cache = self.root / "row_cache"
        self.runs = self.root / "runs"
        self.pipeline_runs = 0

    def tearDown(self):
        self.tmp.cleanup()

    def credential(self, name: str, settings: dict[str, str]) -> Path:
        path = self.root / f"{name}.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in {"AGENTSWE_RUNTIME_API_KEY": KEY, **settings}.items()))
        return path

    def fake_collect_eval(self, job, cases):
        self.pipeline_runs += 1
        rows = []
        for case_id in cases:
            result = self.root / "eval" / f"{self.pipeline_runs}-{case_id}.json"
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text(json.dumps({"case_id": case_id, "score": 40}))
            rows.append({"case_id": case_id, "score": 40, "contract_valid": True, "validity_gate": True,
                         "official_evaluation": True, "infrastructure_failure": False,
                         "native_task_available": True, "eval_result": str(result)})
        return {"cases": rows}

    def run_phase(self, run_id: str, credential: Path, benchmark_id: str = TAU3, env: dict | None = None) -> dict:
        argv = ["adapter.py", "--benchmark-id", benchmark_id, "--candidate", str(self.candidate),
                "--run-id", run_id, "--runs-dir", str(self.runs), "--jobs-dir", str(self.root / "jobs"),
                "--credential-file", str(credential), "--row-cache-dir", str(self.cache)]
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTSWE_RUNTIME_", "AGENTSWE_JUDGE_",
                                                                             "AGENTSWE_SEARCH_", "AGENTSWE_OSWORLD_"))}
        clean.update(env or {})
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.dict(adapter.BENCHMARKS, {benchmark_id: self.bench}), \
                mock.patch.object(adapter, "stage_job", return_value=(self.root / "config.json", self.root / "job")), \
                mock.patch.object(adapter, "run_harbor", return_value={"exit_code": 0}), \
                mock.patch.object(adapter, "collect_candidate", return_value={"cases": []}), \
                mock.patch.object(adapter, "cleanup_job_networks"), \
                mock.patch.object(adapter, "run_tau3_native", return_value={}), \
                mock.patch.object(adapter, "run_osworld_native", return_value={}), \
                mock.patch.object(adapter, "collect_eval", side_effect=self.fake_collect_eval), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(adapter.main(), 0)
        return json.loads((self.runs / run_id / "score_summary.json").read_text())

    def test_same_configuration_reuses_the_rows(self):
        first = self.run_phase("a1", self.credential("a1", CONFIG_A))
        self.assertEqual((first["row_cache_hits"], first["row_cache_misses"], self.pipeline_runs), (0, 2, 1))
        again = self.run_phase("a2", self.credential("a2", CONFIG_A))
        self.assertEqual((again["row_cache_hits"], again["row_cache_misses"], self.pipeline_runs), (2, 0, 1))
        self.assertEqual(again["candidate_run"], {"cache_only": True})
        self.assertEqual([r["score"] for r in again["cases"]], [40, 40])

    def test_another_configuration_scores_the_rows_again(self):
        self.run_phase("base", self.credential("base", CONFIG_A))
        changes = {"AGENTSWE_RUNTIME_MODEL": "model-b", "AGENTSWE_RUNTIME_EFFORT": "low",
                   "AGENTSWE_RUNTIME_RESPONSES_URL": "https://provider-b.example/v1/responses",
                   "AGENTSWE_JUDGE_EFFORT": "high"}
        for name, value in changes.items():
            with self.subTest(setting=name):
                before = self.pipeline_runs
                summary = self.run_phase(f"b-{name.lower()}", self.credential(name, {**CONFIG_A, name: value}))
                self.assertEqual((summary["row_cache_hits"], summary["row_cache_misses"]), (0, 2))
                self.assertEqual(self.pipeline_runs, before + 1)

    def test_osworld_scoring_settings_are_part_of_the_key(self):
        cred = self.credential("osw", CONFIG_A)
        self.run_phase("o1", cred, OSWORLD, {"AGENTSWE_OSWORLD_SCHEMA_STRICT": "1"})
        self.assertEqual(self.run_phase("o2", cred, OSWORLD, {"AGENTSWE_OSWORLD_SCHEMA_STRICT": "1"})["row_cache_hits"], 2)
        self.assertEqual(self.run_phase("o3", cred, OSWORLD, {"AGENTSWE_OSWORLD_SCHEMA_STRICT": "0"})["row_cache_hits"], 0)
        self.assertEqual(self.run_phase("o4", cred, OSWORLD, {"AGENTSWE_OSWORLD_SCHEMA_STRICT": "1",
                                                               "AGENTSWE_OSWORLD_EFFORT": "low"})["row_cache_hits"], 0)
        # placement-only settings leave the key alone
        self.assertEqual(self.run_phase("o5", cred, OSWORLD, {"AGENTSWE_OSWORLD_SCHEMA_STRICT": "1",
                                                               "OSWORLD_PROVIDER_CPUSET": "0-3"})["row_cache_hits"], 2)

    def test_entries_under_the_old_key_are_not_reused(self):
        self.run_phase("old", self.credential("old", CONFIG_A))
        for path in list(self.cache.rglob("*.json")):  # move every entry to where the old key put it
            record = json.loads(path.read_text())
            old_key = hashlib.sha256(json.dumps({k: record[k] for k in (
                "candidate_digest", "case_id", "benchmark_digest", "protocol_source_digest")},
                sort_keys=True).encode()).hexdigest()
            record["cache_key"] = old_key
            path.unlink()
            target = self.cache / old_key[:2] / f"{old_key}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record))
        summary = self.run_phase("new", self.credential("new", CONFIG_A))
        self.assertEqual((summary["row_cache_hits"], summary["row_cache_misses"]), (0, 2))

    def test_stored_row_and_manifest(self):
        self.run_phase("s1", self.credential("s1", CONFIG_A))
        records = [json.loads(p.read_text()) for p in self.cache.rglob("*.json")]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(set(record), RECORD_FIELDS)
            self.assertEqual(record["schema_version"], "1.0")
            self.assertEqual(record["case"], {"case_id": record["case_id"], "score": 40, "contract_valid": True,
                                              "validity_gate": True, "official_evaluation": True,
                                              "infrastructure_failure": False, "native_task_available": True})
            self.assertEqual(record["eval_result"], {"case_id": record["case_id"], "score": 40})
        manifest = (self.runs / "s1" / "row_cache_manifest.json").read_text()
        self.assertEqual(json.loads(manifest)["evaluation_config"]["AGENTSWE_RUNTIME_MODEL"], "model-a")
        self.assertNotIn(KEY, manifest)
        self.assertNotIn(KEY, "".join(p.read_text() for p in self.cache.rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
