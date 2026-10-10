from src.selector import select_first_max


def test_first_equal_maximum_is_stable():
    selected, tied = select_first_max(
        [{"id": "alpha", "score": 0.8}, {"id": "beta", "score": 0.8}]
    )
    assert selected == "alpha"
    assert tied is True
