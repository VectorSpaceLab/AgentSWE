from src.encoder import encode
from src.gate import apply_gate


def evaluate(values, gate_enabled):
    outputs = apply_gate(encode(values), gate_enabled)
    return outputs, sum(outputs) / len(outputs)
