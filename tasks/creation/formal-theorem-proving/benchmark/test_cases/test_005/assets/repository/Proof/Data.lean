namespace Hidden.Pipeline

def normalize : List Nat → List Nat
  | [] => []
  | value :: rest => (value + 0) :: normalize rest

def measure : List Nat → Nat
  | [] => 0
  | _ :: rest => Nat.succ (measure rest)

def checksum : List Nat → Nat
  | [] => 0
  | value :: rest => value + checksum rest

def increment : List Nat → List Nat
  | [] => []
  | value :: rest => Nat.succ value :: increment rest

def appendMarker (marker : Nat) (values : List Nat) : List Nat :=
  values ++ [marker]

def summary (values : List Nat) : Nat × Nat :=
  (measure values, checksum values)

theorem measure_nil : measure [] = 0 := by
  rfl

theorem checksum_nil : checksum [] = 0 := by
  rfl

theorem summary_fst (values : List Nat) : (summary values).1 = measure values := by
  rfl

theorem summary_snd (values : List Nat) : (summary values).2 = checksum values := by
  rfl

namespace Distractor

def normalize (values : List Nat) : List Nat := values ++ [0]

theorem normalize_eq (values : List Nat) :
    normalize values = values ++ [0] := by
  rfl

end Distractor
end Hidden.Pipeline
