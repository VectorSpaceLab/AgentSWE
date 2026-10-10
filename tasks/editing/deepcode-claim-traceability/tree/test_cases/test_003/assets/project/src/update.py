def decay(value, learning_rate, steps):
    for _ in range(steps):
        value *= 1.0 - learning_rate
    return value
