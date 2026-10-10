from src.pipeline import split_and_standardize


def test_train_statistics_exclude_test_outliers():
    mean, _, train, test = split_and_standardize([1, 2, 3, 4, 5, 6, 100, 200], 6)
    assert mean == 3.5
    assert abs(sum(train) / len(train)) < 1e-12
    assert min(test) > max(train)
