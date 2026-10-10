from src.sampler import sample_counts, softmax


def test_probabilities_normalized():
    probabilities = softmax([0.0, 1.0, 2.0])
    assert abs(sum(probabilities) - 1.0) < 1e-12
    assert all(value >= 0 for value in probabilities)


def test_seed_is_deterministic():
    assert sample_counts([0.0, 1.0, 2.0], 50, 7) == sample_counts([0.0, 1.0, 2.0], 50, 7)
