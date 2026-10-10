# Ordered candidate selection

## Pseudocode

ALG-2 scans the candidates in input order, keeps the one with the maximum
score, and returns the selected identifier. The pseudocode compares the new
score with the current best score, but does not say what happens when they
are equal. GAP-2 is this omitted tie behavior.

## Protocol

Read `data/candidates.json`. Use seed `11` and the command `python run_experiment.py
--input data/candidates.json --output artifacts/selection.json --seed 11`.

## Claims

CLM-2: For a fixed ordered input and seed, the selection is deterministic. The paper
does not state which identifier wins an exact tie.
