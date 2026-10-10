from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_ROOT = Path(__file__).resolve().parents[1] / "manifests"
CANONICAL_TEST_CASES_ROOT = ROOT / "test_cases"

CASE_SPECS: dict[str, dict[str, Any]] = {
    "dev_001": {
        "kind": "dev", "tenant": "lab-alpha", "key": "affine-public-v1",
        "ids": ["EQ-1", "ALG-1", "CLM-1"], "claim": "CLM-1",
        "mappings": ["src/normalizer.py", "config.json", "tests/test_normalizer.py", "artifacts/result.json"],
        "statuses": ["complete"], "seeds": [0], "artifact": "result.json",
        "provenance": ["paper_fact", "code", "config", "test", "artifact"],
        "project": "affine-project", "scenario": "nominal",
    },
    "dev_002": {
        "kind": "dev", "tenant": "lab-beta", "key": "selection-public-v1",
        "ids": ["ALG-2", "GAP-2", "CLM-2"], "claim": "CLM-2",
        "mappings": ["src/selector.py", "tests/test_selector.py", "artifacts/selection.json"],
        "statuses": ["partial"], "seeds": [11], "artifact": "selection.json",
        "provenance": ["paper_fact", "implementation_assumption", "external_evidence", "code", "test", "artifact"],
        "terms": ["tie", "first", "alternative", "external"],
        "project": "selection-project", "scenario": "pause_resume",
    },
    "test_001": {
        "kind": "test", "tenant": "decay-team", "key": "decay-broadcast-v1",
        "ids": ["EQ-B1", "ALG-B1", "CLM-B1"], "claim": "CLM-B1",
        "mappings": ["src/decay.py", "config.json", "tests/test_decay.py", "artifacts/decay.json"],
        "statuses": ["complete"], "seeds": [3], "artifact": "decay.json",
        "provenance": ["paper_fact", "code", "config", "test", "artifact"],
        "project": "decay-project", "scenario": "concurrent_retry",
    },
    "test_002": {
        "kind": "test", "tenant": "sampling-team", "key": "sampler-three-seed-v1",
        "ids": ["ALG-S2", "GAP-N2", "CLM-S2"], "claim": "CLM-S2",
        "mappings": ["src/sampler.py", "tests/test_sampler.py", "artifacts/samples.json"],
        "statuses": ["partial"], "seeds": [7, 19, 31], "artifact": "samples.json",
        "provenance": ["paper_fact", "implementation_assumption", "code", "test", "artifact"],
        "terms": ["normal", "softmax", "alternative", "weight"],
        "project": "sampling-project", "scenario": "response_loss",
    },
    "test_003": {
        "kind": "test", "tenant": "tenant-red", "key": "update-conflict-v1",
        "ids": ["HP-LR", "EQ-C3", "CLM-C3"], "claim": "CLM-C3",
        "mappings": ["src/update.py", "config.json", "tests/test_update.py", "artifacts/update.json"],
        "statuses": ["partial"], "seeds": [5], "artifact": "update.json",
        "provenance": ["paper_fact", "implementation_assumption", "code", "config", "artifact"],
        "terms": ["0.1", "0.01", "main", "appendix"],
        "project": "update-project", "scenario": "scope_conflict",
    },
    "test_004": {
        "kind": "test", "tenant": "preprocess-team", "key": "preprocess-order-v1",
        "ids": ["DATA-SPLIT", "PREP-ORDER", "CLM-D4"], "claim": "CLM-D4",
        "mappings": ["src/pipeline.py", "config.json", "tests/test_pipeline.py", "data/values.csv", "artifacts/preprocessing.json"],
        "statuses": ["complete"], "seeds": [13], "artifact": "preprocessing.json",
        "provenance": ["paper_fact", "code", "config", "test", "artifact"],
        "terms": ["split", "fit_train_statistics", "transform_train", "transform_test"],
        "project": "preprocess-project", "scenario": "corrupt_reconcile",
    },
    "test_005": {
        "kind": "test", "tenant": "ablation-team", "key": "gate-ablation-v1",
        "ids": ["ALG-E5", "ABL-GATE", "CLM-A5"], "claim": "CLM-A5",
        "mappings": ["src/encoder.py", "src/gate.py", "src/model.py", "configs/full.json", "configs/without_gate.json", "tests/test_model.py", "artifacts/ablation.json"],
        "statuses": ["complete"], "seeds": [23], "artifact": "ablation.json",
        "provenance": ["paper_fact", "code", "config", "test", "artifact"],
        "terms": ["full", "without_gate", "ablation"],
        "project": "ablation-project", "scenario": "cancel_fence",
    },
    "test_006": {
        "kind": "test", "tenant": "citation-team", "key": "missing-citation-v1",
        "ids": ["EQ-K6", "CITE-K6", "GAP-K6", "CLM-K6"], "claim": "CLM-K6",
        "mappings": ["src/score.py", "tests/test_score.py"],
        "statuses": ["blocked"], "seeds": [], "artifact": None,
        "provenance": ["paper_fact", "code", "test"],
        "terms": ["syn-cite-17", "kappa", "missing"], "blocked": True,
        "project": "citation-project", "scenario": "policy_change",
    },
}

PUBLIC_CASES = ["dev_001", "dev_002"]
HIDDEN_CASES = [f"test_{index:03d}" for index in range(1, 7)]


def case_directory(case_id: str) -> Path:
    kind = "dev_cases" if CASE_SPECS[case_id]["kind"] == "dev" else "test_cases"
    return ROOT / kind / case_id


def load_manifest(case_id: str) -> dict[str, Any]:
    return json.loads((MANIFEST_ROOT / f"{case_id}.json").read_text(encoding="utf-8"))


def canonical_hidden_case(case_id: str, root: Path | None = None) -> Path:
    """Resolve one evaluator-owned canonical hidden case, fail closed.

    Callers select by the locked case ID, never by an arbitrary case path.
    ``root`` exists only so an evaluator can mount the canonical test_cases
    tree at another absolute location; the Candidate/Builder never controls it.
    """
    if case_id not in HIDDEN_CASES:
        raise ValueError(f"unknown hidden case: {case_id}")
    raw_root = root or CANONICAL_TEST_CASES_ROOT
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError(f"canonical test_cases root is unavailable: {raw_root}")
    canonical_root = raw_root.resolve()
    actual = sorted(path.name for path in canonical_root.iterdir() if path.is_dir())
    if actual != HIDDEN_CASES:
        raise ValueError(f"canonical hidden inventory mismatch: {actual}")
    case = canonical_root / case_id
    if case.is_symlink() or case.resolve().parent != canonical_root:
        raise ValueError(f"hidden case is not a direct canonical child: {case_id}")
    if not (case / "input.md").is_file() or (case / "input.md").is_symlink():
        raise ValueError(f"canonical hidden input is unavailable: {case_id}")
    project = case / "assets" / "project"
    if not project.is_dir() or project.is_symlink():
        raise ValueError(f"canonical hidden project is unavailable: {case_id}")
    return case
