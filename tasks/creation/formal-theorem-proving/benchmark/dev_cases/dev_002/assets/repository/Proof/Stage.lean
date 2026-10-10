namespace Training.Pipeline

def pad (n : Nat) : Nat := n + 0

def advance (n : Nat) : Nat := Nat.succ n

def stage (n : Nat) : Nat := pad (advance n)

def twice (f : α → α) (x : α) : α := f (f x)

def cost (n : Nat) : Nat := n + 3

theorem pad_eq (n : Nat) : pad n = n := by
  simp [pad]

theorem advance_eq (n : Nat) : advance n = n + 1 := by
  simp [advance]

theorem cost_injective {a b : Nat} (h : cost a = cost b) : a = b := by
  exact Nat.add_right_cancel h

theorem twice_id (f : α → α) (h : ∀ x, f x = x) (x : α) : twice f x = x := by
  simp [twice, h]

namespace Distractor

def pad (n : Nat) : Nat := n + 1

theorem pad_eq (n : Nat) : pad n = Nat.succ n := by
  simp [pad]

end Distractor

structure Ticket where
  serial : Nat
  priority : Nat
deriving Repr, DecidableEq

def Ticket.raise (ticket : Ticket) : Ticket :=
  { ticket with priority := advance ticket.priority }

theorem Ticket.raise_serial (ticket : Ticket) : ticket.raise.serial = ticket.serial := by
  rfl

end Training.Pipeline
