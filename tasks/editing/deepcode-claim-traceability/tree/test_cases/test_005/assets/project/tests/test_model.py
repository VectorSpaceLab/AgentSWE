from src.encoder import encode
from src.gate import apply_gate
from src.model import evaluate


def test_modules_and_ablation_are_distinct():
    assert encode([1, 2, 3]) == [2, 4, 6]
    assert apply_gate([2, 4, 6], False) == [2, 4, 6]
    assert evaluate([1, 2, 3], True)[1] - evaluate([1, 2, 3], False)[1] == 1
