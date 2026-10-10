from __future__ import annotations

import math


def affine_standardize(values, *, epsilon: float, gamma: float, beta: float):
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    sigma = math.sqrt(variance)
    return [((value - mean) / (sigma + epsilon)) * gamma + beta for value in values]
