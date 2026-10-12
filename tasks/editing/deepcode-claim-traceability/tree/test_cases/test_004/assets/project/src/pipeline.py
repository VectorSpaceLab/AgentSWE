import math


def split_and_standardize(values, train_count):
    train = values[:train_count]
    test = values[train_count:]
    mean = sum(train) / len(train)
    std = math.sqrt(sum((value - mean) ** 2 for value in train) / len(train))
    transform = lambda rows: [(value - mean) / std for value in rows]
    return mean, std, transform(train), transform(test)
