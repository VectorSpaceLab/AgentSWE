import Proof.Facts

namespace Hidden.Pipeline.Candidates

open Hidden.Pipeline

theorem candidate_capacity (values : List Nat) :
    measure (normalize values) = measure values := by
  -- Candidate matching the first sentence of SPEC.md.

def candidate_growth : Prop :=
  ∀ values : List Nat, measure (appendMarker 0 values) = measure values

theorem candidate_checksum (values : List Nat) :
    checksum (values ++ []) = checksum values := by
  -- Candidate matching the second sentence of SPEC.md.

theorem candidate_increment_length (values : List Nat) :
    measure (increment values) = measure values := by
  exact Hidden.Pipeline.Facts.measure_increment values

def candidate_unrelated : Prop := ∀ n : Nat, n + 0 = n

theorem sample_summary : summary [2, 4, 1] = (3, 7) := by
  rfl

end Hidden.Pipeline.Candidates
