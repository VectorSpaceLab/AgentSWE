import Proof.Additive
import Proof.Prefix

namespace Hidden.Ambiguous.Target

open Hidden.Ambiguous
open Additive Prefix

theorem additive_identity (n : Nat) :
    Additive.apply Additive.Action.unit n = n := by
  -- Resolve the intended Action class and namespaced premise.

theorem prefix_identity (xs : List Nat) :
    Prefix.apply Prefix.Action.unit xs = xs := by
  -- A same-named theorem exists in more than one namespace.

theorem additive_sample : Additive.apply 0 9 = 9 := by
  rfl

theorem prefix_sample : Prefix.apply [] [2, 3] = [2, 3] := by
  rfl

def mixedSummary (n : Nat) (xs : List Nat) : Nat × List Nat :=
  (Additive.apply Additive.Action.unit n, Prefix.apply Prefix.Action.unit xs)

theorem mixedSummary_fst (n : Nat) (xs : List Nat) :
    (mixedSummary n xs).1 = n := by
  exact Additive.unit_act n

end Hidden.Ambiguous.Target
