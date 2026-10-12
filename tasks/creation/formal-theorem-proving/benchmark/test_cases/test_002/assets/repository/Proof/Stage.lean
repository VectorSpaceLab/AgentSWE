import Proof.Core

namespace Hidden.Dependency

theorem clear_pad (n : Nat) : pad n = n := by
  -- First target: later declarations depend on this exact theorem.

theorem process_nonzero (n : Nat) : process n ≠ 0 := by
  rw [process, clear_pad, clear_pad]
  exact boost_ne_zero n

theorem audit_payload (n : Nat) : (audit n).1 = process n := by
  rfl

theorem cost_process (n : Nat) : cost (process n) = process n + 3 := by
  rfl

theorem clear_pad_twice (n : Nat) : pad (pad n) = n := by
  rw [clear_pad, clear_pad]

end Hidden.Dependency
