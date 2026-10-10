# Split-before-normalization protocol

DATA-SPLIT assigns rows 0 to 5 to training and rows 6 to 7 to test.
PREP-ORDER is: split, fit the mean and population standard deviation on training
only, transform training, then transform test with the training statistics.

Run seed 13 with `python run_experiment.py --data data/values.csv --config
config.json --output artifacts/preprocessing.json --seed 13`.

CLM-D4: The normalized training values have zero mean and population variance
one. Test values must not influence the fitted preprocessing statistics.
