import Proof.Data

namespace Hidden.Pipeline.Facts

open Hidden.Pipeline

theorem measure_normalize (values : List Nat) :
    measure (normalize values) = measure values := by
  induction values with
  | nil => rfl
  | cons value rest ih => simp [normalize, measure, ih]

theorem checksum_append (left right : List Nat) :
    checksum (left ++ right) = checksum left + checksum right := by
  induction left with
  | nil => simp [checksum]
  | cons value rest ih => simp [checksum, ih, Nat.add_assoc]

theorem checksum_append_empty (values : List Nat) :
    checksum (values ++ []) = checksum values := by
  rw [List.append_nil]

theorem measure_append (left right : List Nat) :
    measure (left ++ right) = measure left + measure right := by
  induction left with
  | nil => simp [measure]
  | cons value rest ih => simp [measure, ih, Nat.succ_add]

theorem measure_increment (values : List Nat) :
    measure (increment values) = measure values := by
  induction values with
  | nil => rfl
  | cons value rest ih => simp [increment, measure, ih]

theorem checksum_singleton (value : Nat) : checksum [value] = value := by
  simp [checksum]

theorem checksum_marker (values : List Nat) (marker : Nat) :
    checksum (appendMarker marker values) = checksum values + marker := by
  simp [appendMarker, checksum_append, checksum_singleton]

end Hidden.Pipeline.Facts
