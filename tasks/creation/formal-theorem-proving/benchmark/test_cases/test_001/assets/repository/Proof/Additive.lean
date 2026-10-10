namespace Hidden.Ambiguous.Additive

class Action (α : Type u) where
  unit : α
  act : α → α → α
  unit_act : ∀ x, act unit x = x

def apply [Action α] (left right : α) : α := Action.act left right

instance natAction : Action Nat where
  unit := 0
  act := Nat.add
  unit_act := Nat.zero_add

theorem unit_act [Action α] (x : α) : apply Action.unit x = x := by
  exact Action.unit_act x

theorem unit_act_nat (x : Nat) : apply Action.unit x = x := by
  exact unit_act x

theorem add_one (x : Nat) : apply 1 x = Nat.succ x := by
  exact Nat.one_add x

theorem reassociate (a b c : Nat) : apply (apply a b) c = apply a (apply b c) := by
  exact Nat.add_assoc a b c

def iterate (n x : Nat) : Nat :=
  match n with
  | 0 => Action.unit
  | Nat.succ k => apply x (iterate k x)

theorem iterate_zero (x : Nat) : iterate 0 x = Action.unit := by
  rfl

end Hidden.Ambiguous.Additive
