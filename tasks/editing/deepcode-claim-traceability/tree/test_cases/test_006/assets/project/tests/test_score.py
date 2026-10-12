import pytest

from src.score import score


def test_missing_citation_remains_explicitly_blocked():
    with pytest.raises(NotImplementedError, match="SYN-CITE-17"):
        score(2.0)
