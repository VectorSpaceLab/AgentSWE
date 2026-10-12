def apply_gate(values, enabled):
    return [value + 1 for value in values] if enabled else list(values)
