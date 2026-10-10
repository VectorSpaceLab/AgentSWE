# Affine normalization

## Method

EQ-1 defines the transform of a vector x:

`y_i = ((x_i - mu) / (sigma + epsilon)) * gamma + beta`.

Here `mu` is the vector mean and `sigma` is the population standard deviation.
ALG-1 applies EQ-1 element-wise and returns a vector with the same shape as x.

## Configuration and protocol

Use epsilon `1e-12`, gamma `2.0`, beta `-1.0`, and seed `0`. Read the input
`data/input.json` and run `python run_experiment.py --config config.json
--input data/input.json --output artifacts/result.json --seed 0`.

## Claims

CLM-1: For x = [1, 2, 3], the transformed output has mean beta (-1.0) and
population variance gamma squared (4.0), within an absolute tolerance of 1e-8.
