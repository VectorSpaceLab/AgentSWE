# Gated modular scoring

ALG-E5 encodes every scalar by multiplying it by 2, and the gate then adds one
when enabled. The model reports the mean output.

ABL-GATE disables the gate while keeping the same encoder, input, and seed.
Run both with `python run_experiment.py --input data/values.json --full
configs/full.json --ablation configs/without_gate.json --output
artifacts/ablation.json --seed 23`.

CLM-A5: On [1,2,3], the full mean is 5, the no-gate mean is 4, and the
gate contribution improves by 1.
