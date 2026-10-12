import Proof.Stage

namespace Training.Pipeline

def trace (n : Nat) : List Nat := [n, stage n, stage (stage n)]

def ticketSummary (ticket : Ticket) : Nat × Nat :=
  (ticket.serial, ticket.priority)

theorem trace_length (n : Nat) : (trace n).length = 3 := by
  rfl

theorem trace_head (n : Nat) : (trace n).head? = some n := by
  rfl

theorem ticketSummary_raise (ticket : Ticket) :
    (ticketSummary ticket.raise).1 = ticket.serial := by
  rfl

theorem cost_advance (n : Nat) : cost (advance n) = Nat.succ n + 3 := by
  rfl

end Training.Pipeline
