import math
import pytest

from src.decay import column_decay


def test_units_and_broadcasting():
    result = column_decay([[1000.0, 2000.0]], [1.0, 2.0])
    assert result[0] == pytest.approx([math.exp(-1), math.exp(-1)])


def test_width_mismatch_is_rejected():
    with pytest.raises(ValueError):
        column_decay([[1.0, 2.0]], [1.0])
