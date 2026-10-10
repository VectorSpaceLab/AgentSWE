import Proof.Stage

namespace Hidden.Dependency

theorem one_pass (n : Nat) : process n = boost n := by
  -- Second target: use clear_pad rather than unfolding arithmetic globally.

theorem two_pass (n : Nat) : process (process n) = boost (boost n) := by
  -- Third target: use the repaired one_pass theorem twice.

theorem two_pass_cost (n : Nat) :
    cost (process (process n)) = cost (boost (boost n)) := by
  -- Fourth target: close only after two_pass is available.

def sampleJob : Job := { payload := 4, attempts := 1 }

theorem sample_attempts : sampleJob.run.attempts = 2 := by
  rfl

theorem sample_payload_nonzero : sampleJob.run.payload ≠ 0 := by
  exact process_nonzero 4

end Hidden.Dependency
