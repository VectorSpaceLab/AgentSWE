import Proof.Sequence

namespace Hidden.Sequence

theorem mirror_append (xs ys : List α) :
    mirror (xs ++ ys) = mirror ys ++ mirror xs := by
  induction xs with
  | nil => simp [mirror]
  | cons x xs ih => simp [mirror, ih, List.append_assoc]

theorem mirror_singleton (x : α) : mirror [x] = [x] := by
  rfl

theorem mirror_pair (x y : α) : mirror [x, y] = [y, x] := by
  rfl

theorem mirror_pushBack (xs : List α) (x : α) :
    mirror (pushBack xs x) = x :: mirror xs := by
  simp [pushBack, mirror_append, mirror_singleton]

theorem mirror_length (xs : List α) : (mirror xs).length = xs.length := by
  induction xs with
  | nil => rfl
  | cons x xs ih => simp [mirror, ih]

theorem rotateLeft_length (xs : List α) : (rotateLeft xs).length = xs.length := by
  cases xs with
  | nil => rfl
  | cons x xs => simp [rotateLeft]

theorem headOr_singleton (fallback x : α) : headOr fallback [x] = x := by
  rfl

end Hidden.Sequence
