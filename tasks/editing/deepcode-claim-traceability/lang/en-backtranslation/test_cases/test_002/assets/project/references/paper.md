# Exponentially weighted sampling

ALG-S2 computes a weight `w_j = exp(score_j)` for every item and draws 200 items
from those weights. GAP-N2: the pseudocode does not say how the weights become
the probability distribution accepted by the categorical sampler.

Run seeds 7, 19, and 31 with `python run_experiment.py --input data/scores.json
--output artifacts/samples.json --draws 200 --seeds 7 19 31`.

CLM-S2: Repeated runs with the same seed sequence produce identical per-seed
counts, and the item with the largest score has the largest count after
aggregating the three runs.
