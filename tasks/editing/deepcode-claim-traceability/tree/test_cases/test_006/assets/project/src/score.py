def kappa(_value):
    raise NotImplementedError("SYN-CITE-17 definition was not provided")


def score(value):
    transformed = kappa(value)
    return transformed / (1 + abs(transformed))
