namespace Hidden.Workflow

inductive Phase where
  | idle
  | queued
  | running
  | done
deriving Repr, DecidableEq

open Phase

def tick : Phase → Phase
  | idle => queued
  | queued => running
  | running => done
  | done => done

def eligible : Phase → Prop
  | idle => False
  | queued => True
  | running => True
  | done => False

def priority : Phase → Nat
  | idle => 0
  | queued => 2
  | running => 3
  | done => 4

def terminal : Phase → Bool
  | done => true
  | _ => false

def remaining : Phase → Nat
  | idle => 3
  | queued => 2
  | running => 1
  | done => 0

def snapshot (phase : Phase) : Nat × Bool :=
  (priority phase, terminal phase)

theorem tick_idle : tick idle = queued := by
  rfl

theorem tick_queued : tick queued = running := by
  rfl

theorem tick_running : tick running = done := by
  rfl

theorem tick_done : tick done = done := by
  rfl

theorem terminal_done : terminal done = true := by
  rfl

end Hidden.Workflow
