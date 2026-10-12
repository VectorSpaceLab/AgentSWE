namespace Hidden.Sequence

def mirror : List α → List α
  | [] => []
  | x :: xs => mirror xs ++ [x]

def join (left right : List α) : List α := left ++ right

def pushBack (xs : List α) (x : α) : List α := xs ++ [x]

def headOr (fallback : α) : List α → α
  | [] => fallback
  | x :: _ => x

def rotateLeft : List α → List α
  | [] => []
  | x :: xs => xs ++ [x]

def duplicate : List α → List α
  | [] => []
  | x :: xs => x :: x :: duplicate xs

theorem mirror_nil : mirror ([] : List α) = [] := by
  rfl

theorem mirror_cons (x : α) (xs : List α) :
    mirror (x :: xs) = mirror xs ++ [x] := by
  rfl

theorem join_nil_left (xs : List α) : join [] xs = xs := by
  rfl

theorem pushBack_eq (xs : List α) (x : α) : pushBack xs x = xs ++ [x] := by
  rfl

theorem duplicate_length (xs : List α) : (duplicate xs).length = xs.length + xs.length := by
  induction xs with
  | nil => rfl
  | cons x xs ih => simp [duplicate, ih, Nat.add_assoc, Nat.add_comm, Nat.add_left_comm]

namespace Distractor

def mirror (xs : List α) : List α := xs

theorem mirror_append (xs ys : List α) : mirror (xs ++ ys) = mirror xs ++ mirror ys := by
  rfl

end Distractor

end Hidden.Sequence
