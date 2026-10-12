from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    min_claims: int
    decisions: tuple[str, ...]
    issue_groups: tuple[tuple[str, tuple[str, ...]], ...]
    banned_writeup: tuple[str, ...]
    required_writeup_groups: tuple[tuple[str, ...], ...]
    numeric_evidence_groups: tuple[tuple[str, ...], ...]
    experiment_ids: tuple[str, ...]
    config_paths: tuple[str, ...]
    seeds: tuple[int, ...]
    dataset_tokens: tuple[str, ...]
    metric_tokens: tuple[str, ...]
    unresolved_groups: tuple[tuple[str, ...], ...]
    preserve_groups: tuple[tuple[str, ...], ...] = ()


SPECS = {
    "dev_001": CaseSpec(
        case_id="dev_001",
        min_claims=2,
        decisions=("release_with_revisions",),
        issue_groups=(("numeric_mismatch", ("numeric mismatch", "magnitude mismatch", "claim artifact mismatch", "contradict", "incorrect value")),),
        banned_writeup=("improves accuracy by 5.0",),
        required_writeup_groups=(("0.5 percentage", "0.5 points", "0.005"),),
        numeric_evidence_groups=(("0.005", "0.5 percentage", "0.5 points"),),
        experiment_ids=("exp-baseline-001", "exp-nova-001"),
        config_paths=("baseline_config.json", "nova_config.json"),
        seeds=(11, 22, 33),
        dataset_tokens=("mini_cls_v1", "split-public-a"),
        metric_tokens=("accuracy",),
        unresolved_groups=(),
        preserve_groups=(("fixed public mini-validation split",),),
    ),
    "dev_002": CaseSpec(
        case_id="dev_002",
        min_claims=2,
        decisions=("block",),
        issue_groups=(("insufficient_seeds", ("insufficient seed", "single seed", "one seed", "seed coverage", "stability evidence")),),
        banned_writeup=("stable across random initializations",),
        required_writeup_groups=(("1.2 percentage", "1.2 points", "0.012"), ("seed 17", "one seed", "single seed")),
        numeric_evidence_groups=(("0.012", "1.2 percentage", "1.2 points"),),
        experiment_ids=("exp-baseline-single", "exp-cedar-single"),
        config_paths=("baseline_config.json", "cedar_config.json"),
        seeds=(17,),
        dataset_tokens=("mini_cls_v1", "split-public-b"),
        metric_tokens=("accuracy", "stability"),
        unresolved_groups=(("more seeds", "additional seeds", "at least 3", "three seeds", "rerun"),),
    ),
    "test_001": CaseSpec(
        case_id="test_001",
        min_claims=2,
        decisions=("block",),
        issue_groups=(
            ("data_leakage", ("data leakage", "leakage", "patient_id", "identifier feature")),
            ("split_mismatch", ("split mismatch", "different split", "row-split-b", "patient-split-a", "split identity")),
        ),
        banned_writeup=("generalizes to unseen patients", "improves accuracy by 4.0"),
        required_writeup_groups=(("50.0 percent", "50 percent", "0.50"),),
        numeric_evidence_groups=(("0.74", "74.0", "74 percent"), ("0.50", "50.0", "50 percent")),
        experiment_ids=("exp-base", "exp-aurora", "exp-shuffle"),
        config_paths=("base_config.json", "aurora_config.json", "shuffle_config.json"),
        seeds=(3,),
        dataset_tokens=("patients-mini", "patient-split-a", "row-split-b"),
        metric_tokens=("accuracy",),
        unresolved_groups=(("remove patient", "leakage-free", "patient-level", "matching split", "rerun"),),
        preserve_groups=(("label-shuffle control", "label shuffle control"),),
    ),
    "test_002": CaseSpec(
        case_id="test_002",
        min_claims=2,
        decisions=("block",),
        issue_groups=(
            ("best_seed_selection", ("best seed", "seed selection", "selective reporting", "included_seeds", "only seed 1")),
            ("missing_negative_results", ("missing negative", "omitted negative", "negative seeds", "seeds 3", "seeds 4", "selective deletion")),
        ),
        banned_writeup=("consistently improves", "3.0 percentage points", "no completed run produced a negative"),
        required_writeup_groups=(),
        numeric_evidence_groups=(("0.003", "0.3 percentage", "0.3 points"),),
        experiment_ids=("exp-birch-multiseed",),
        config_paths=("config.json", "reported_summary.json"),
        seeds=(1, 2, 3, 4, 5),
        dataset_tokens=("mini-text", "fixed-c"),
        metric_tokens=("f1",),
        unresolved_groups=(("all seeds", "full seed", "report negative", "rerun"),),
    ),
    "test_003": CaseSpec(
        case_id="test_003",
        min_claims=2,
        decisions=("block",),
        issue_groups=(
            ("stale_or_overwritten", ("stale artifact", "overwritten", "artifact provenance", "later run", "exp-later-9")),
            ("checksum_mismatch", ("checksum mismatch", "sha-256 mismatch", "hash mismatch", "integrity")),
            ("run_identity_mismatch", ("run id mismatch", "run identity", "exp-archive-7", "exp-later-9")),
        ),
        banned_writeup=("reaches 82.0", "captured immediately after that run"),
        required_writeup_groups=(),
        numeric_evidence_groups=(("0.61", "61.0", "61 percent"),),
        experiment_ids=("exp-archive-7", "exp-later-9"),
        config_paths=("config.json", "run.log", "raw_results.json"),
        seeds=(9,),
        dataset_tokens=("archive-mini", "archive-split-d"),
        metric_tokens=("accuracy",),
        unresolved_groups=(("recover original", "immutable artifact", "rerun", "restore artifact"),),
    ),
    "test_004": CaseSpec(
        case_id="test_004",
        min_claims=2,
        decisions=("block",),
        issue_groups=(
            ("unequal_budget", ("unequal budget", "budget mismatch", "different training budget", "100", "200")),
            ("ineffective_ablation", ("ineffective ablation", "ablation not disabled", "context_module_enabled=true", "still enabled", "invalid ablation")),
        ),
        banned_writeup=("fairly improves", "equally trained baseline", "proving that the module is essential"),
        required_writeup_groups=(),
        numeric_evidence_groups=(("0.76", "76.0", "6.0 percentage"), ("0.74", "74.0", "2.0")),
        experiment_ids=("exp-base", "exp-delta", "exp-ablate"),
        config_paths=("baseline_config.json", "delta_config.json", "ablation_config.json", "ablation.log"),
        seeds=(5,),
        dataset_tokens=("budget-mini", "split-e"),
        metric_tokens=("accuracy",),
        unresolved_groups=(("equal budget", "matched budget", "disable context", "valid ablation", "rerun"),),
    ),
    "test_005": CaseSpec(
        case_id="test_005",
        min_claims=3,
        decisions=("block",),
        issue_groups=(
            ("invalid_significance", ("not significant", "invalid significance", "p=0.704", "p value 0.704", "false significance")),
            ("high_variance", ("high variance", "variance exceeds", "standard deviation", "0.05")),
        ),
        banned_writeup=("significantly outperforms", "p < 0.01", "has low variance"),
        required_writeup_groups=(("1.0 percentage", "1.0 point", "0.01"),),
        numeric_evidence_groups=(("0.704",), ("0.05",), ("0.01", "1.0 percentage")),
        experiment_ids=("exp-ember-stats",),
        config_paths=("config.json", "statistical_test.json", "raw_results.json"),
        seeds=(10, 20, 30, 40, 50),
        dataset_tokens=("variance-mini", "split-f"),
        metric_tokens=("accuracy", "paired_two_sided_t"),
        unresolved_groups=(("more seeds", "additional seeds", "variance", "significance", "rerun"),),
    ),
    "test_006": CaseSpec(
        case_id="test_006",
        min_claims=2,
        decisions=("block",),
        issue_groups=(
            ("config_log_mismatch", ("config log mismatch", "config/log", "learning_rate", "0.01", "0.1")),
            ("checksum_mismatch", ("checksum mismatch", "sha-256 mismatch", "hash mismatch", "integrity")),
            ("figure_raw_mismatch", ("figure mismatch", "figure/raw", "figure 1", "0.90", "0.75")),
        ),
        banned_writeup=("reaches 90.0", "exactly reflects the raw experiment artifact"),
        required_writeup_groups=(),
        numeric_evidence_groups=(("0.75", "75.0", "75 percent"), ("0.01",), ("0.1",)),
        experiment_ids=("exp-fjord",),
        config_paths=("config.json", "run.log", "figure_1.csv", "raw_results.json"),
        seeds=(12,),
        dataset_tokens=("integrity-mini", "split-g"),
        metric_tokens=("accuracy",),
        unresolved_groups=(("regenerate figure", "reconcile config", "restore config", "correct checksum", "rerun"),),
    ),
}
