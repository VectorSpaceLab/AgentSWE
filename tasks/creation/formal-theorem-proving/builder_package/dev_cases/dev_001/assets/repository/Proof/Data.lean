namespace Training.Warehouse

inductive Batch where
  | empty
  | put (weight : Nat) (rest : Batch)
deriving Repr, DecidableEq

namespace Batch

def merge : Batch → Batch → Batch
  | empty, right => right
  | put weight rest, right => put weight (merge rest right)

def mass : Batch → Nat
  | empty => 0
  | put weight rest => weight + mass rest

def count : Batch → Nat
  | empty => 0
  | put _ rest => Nat.succ (count rest)

def mapWeight (f : Nat → Nat) : Batch → Batch
  | empty => empty
  | put weight rest => put (f weight) (mapWeight f rest)

def allPositive : Batch → Bool
  | empty => true
  | put weight rest => decide (0 < weight) && allPositive rest

def singleton (weight : Nat) : Batch := put weight empty

def sample : Batch := put 2 (put 5 (singleton 3))

end Batch

namespace Shadow

def mass (batch : Batch) : Nat := Batch.count batch

theorem mass_empty : mass Batch.empty = 0 := by
  rfl

end Shadow

end Training.Warehouse
