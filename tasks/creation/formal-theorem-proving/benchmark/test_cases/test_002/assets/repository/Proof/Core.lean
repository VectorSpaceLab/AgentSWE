namespace Hidden.Dependency

def pad (n : Nat) : Nat := n + 0

def boost (n : Nat) : Nat := Nat.succ n

def process (n : Nat) : Nat := pad (boost (pad n))

def cost (n : Nat) : Nat := n + 3

def audit (n : Nat) : Nat × Nat := (process n, cost n)

theorem boost_ne_zero (n : Nat) : boost n ≠ 0 := by
  exact Nat.succ_ne_zero n

theorem cost_eq (n : Nat) : cost n = n + 3 := by
  rfl

theorem audit_snd (n : Nat) : (audit n).2 = cost n := by
  rfl

namespace Distractor

def pad (n : Nat) : Nat := n + 1

theorem clear_pad (n : Nat) : pad n = boost n := by
  rfl

end Distractor

structure Job where
  payload : Nat
  attempts : Nat
deriving Repr, DecidableEq

def Job.run (job : Job) : Job :=
  { payload := process job.payload, attempts := boost job.attempts }

theorem Job.run_attempts (job : Job) : job.run.attempts = boost job.attempts := by
  rfl

end Hidden.Dependency
