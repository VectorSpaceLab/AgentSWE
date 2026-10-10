import math


def column_decay(dt_ms, tau_seconds):
    if any(len(row) != len(tau_seconds) for row in dt_ms):
        raise ValueError("matrix width must match tau vector")
    return [
        [math.exp(-milliseconds / (1000.0 * tau)) for milliseconds, tau in zip(row, tau_seconds)]
        for row in dt_ms
    ]
