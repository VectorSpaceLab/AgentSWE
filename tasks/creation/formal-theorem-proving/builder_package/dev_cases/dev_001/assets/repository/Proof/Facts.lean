import Proof.Data

namespace Training.Warehouse
namespace Batch

theorem merge_empty_right (batch : Batch) : merge batch empty = batch := by
  induction batch with
  | empty => rfl
  | put weight rest ih => simp [merge, ih]

theorem mass_empty : mass empty = 0 := by
  rfl

theorem mass_put (weight : Nat) (rest : Batch) :
    mass (put weight rest) = weight + mass rest := by
  rfl

theorem mass_merge (left right : Batch) :
    mass (merge left right) = mass left + mass right := by
  induction left with
  | empty => simp [merge, mass]
  | put weight rest ih => simp [merge, mass, ih, Nat.add_assoc]

theorem count_merge (left right : Batch) :
    count (merge left right) = count left + count right := by
  induction left with
  | empty => simp [merge, count]
  | put weight rest ih => simp [merge, count, ih, Nat.succ_add]

theorem mapWeight_merge (f : Nat → Nat) (left right : Batch) :
    mapWeight f (merge left right) = merge (mapWeight f left) (mapWeight f right) := by
  induction left with
  | empty => rfl
  | put weight rest ih => simp [merge, mapWeight, ih]

theorem count_sample : count sample = 3 := by
  rfl

theorem sample_positive : allPositive sample = true := by
  decide

end Batch
end Training.Warehouse
