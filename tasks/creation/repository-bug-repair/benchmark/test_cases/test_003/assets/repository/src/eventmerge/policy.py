from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergePolicy:
    maximum_sources: int = 256
    require_exact_ids: bool = True
    preserve_source_ties: bool = True

    def validate(self) -> None:
        if self.maximum_sources <= 0:
            raise ValueError("maximum_sources must be positive")
        if not self.require_exact_ids:
            raise ValueError("only exact event identity is supported")
        if not self.preserve_source_ties:
            raise ValueError("source-order ties must be preserved")
