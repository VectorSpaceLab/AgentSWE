import Proof.Laws

namespace Hidden.Sequence.Target

open Hidden.Sequence

theorem mirror_involutive (xs : List α) : mirror (mirror xs) = xs := by
  -- Use structural induction and only controlled rewrites.

theorem mirrored_pair : mirror (mirror [4, 7]) = [4, 7] := by
  exact mirror_involutive [4, 7]

def canonical (xs : List α) : List α := mirror (mirror xs)

theorem canonical_eq (xs : List α) : canonical xs = xs := by
  exact mirror_involutive xs

theorem canonical_length (xs : List α) : (canonical xs).length = xs.length := by
  rw [canonical_eq]

end Hidden.Sequence.Target
