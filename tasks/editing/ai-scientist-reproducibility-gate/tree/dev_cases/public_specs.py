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
}
