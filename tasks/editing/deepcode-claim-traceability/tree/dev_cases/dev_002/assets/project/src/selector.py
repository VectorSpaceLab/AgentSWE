from __future__ import annotations


def select_first_max(candidates):
    if not candidates:
        raise ValueError("at least one candidate is required")
    best = candidates[0]
    tied = False
    for candidate in candidates[1:]:
        if candidate["score"] > best["score"]:
            best = candidate
            tied = False
        elif candidate["score"] == best["score"]:
            tied = True
    return best["id"], tied
