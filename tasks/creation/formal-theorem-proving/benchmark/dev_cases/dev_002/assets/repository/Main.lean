import Proof.Observations

namespace Training.Pipeline

theorem one_pass (n : Nat) : stage n = advance n := by
  rw [advance_eq]

theorem two_pass (n : Nat) : stage (stage n) = advance (advance n) := by
  -- Complete this dependent target after repairing one_pass.

theorem cost_one_pass (n : Nat) : cost (stage n) = cost (advance n) := by
  rw [one_pass]

def demoTicket : Ticket := { serial := 7, priority := 2 }

theorem demoTicket_serial : demoTicket.raise.serial = 7 := by
  rfl

end Training.Pipeline
