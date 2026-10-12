from src.update import decay


def test_appendix_configuration_result():
    assert abs(decay(1.0, 0.01, 10) - (0.99 ** 10)) < 1e-12
