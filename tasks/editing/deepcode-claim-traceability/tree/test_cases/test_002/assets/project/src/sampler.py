import math
import random


def softmax(scores):
    anchor = max(scores)
    weights = [math.exp(score - anchor) for score in scores]
    total = sum(weights)
    return [weight / total for weight in weights]


def sample_counts(scores, draws, seed):
    probabilities = softmax(scores)
    rng = random.Random(seed)
    counts = [0] * len(scores)
    for selected in rng.choices(range(len(scores)), weights=probabilities, k=draws):
        counts[selected] += 1
    return probabilities, counts
