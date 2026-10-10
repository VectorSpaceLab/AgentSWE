from src.normalizer import affine_standardize


def test_affine_standardize_properties():
    output = affine_standardize([1.0, 2.0, 3.0], epsilon=1e-12, gamma=2.0, beta=-1.0)
    mean = sum(output) / len(output)
    variance = sum((value - mean) ** 2 for value in output) / len(output)
    assert abs(mean + 1.0) < 1e-8
    assert abs(variance - 4.0) < 1e-8
