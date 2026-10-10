namespace Hidden.Ambiguous.Prefix

class Action (α : Type u) where
  unit : α
  act : α → α → α
  unit_act : ∀ x, act unit x = x

def apply [Action α] (left right : α) : α := Action.act left right

instance listAction : Action (List Nat) where
  unit := []
  act := List.append
  unit_act := List.nil_append

theorem unit_act [Action α] (x : α) : apply Action.unit x = x := by
  exact Action.unit_act x

theorem unit_act_list (xs : List Nat) : apply Action.unit xs = xs := by
  exact unit_act xs

theorem append_singleton (xs : List Nat) (x : Nat) :
    apply xs [x] = xs ++ [x] := by
  rfl

def surround (left middle right : List Nat) : List Nat :=
  apply left (apply middle right)

theorem surround_empty (middle : List Nat) :
    surround [] middle [] = middle := by
  change [] ++ (middle ++ []) = middle
  simp

namespace Distractor

theorem unit_act (xs : List Nat) : [] ++ xs = xs := by
  rfl

end Distractor

end Hidden.Ambiguous.Prefix
