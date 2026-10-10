import Proof.Facts

namespace Hidden.Workflow.Target

open Hidden.Workflow Hidden.Workflow.Phase

def conjecture : Prop :=
  ∀ phase : Phase, eligible phase → eligible (tick phase)

def weakerClaim : Prop :=
  eligible queued → eligible (tick queued)

theorem weakerClaim_true : weakerClaim := by
  intro h
  exact Hidden.Workflow.Facts.queued_survives_tick

def terminalStable : Prop :=
  ∀ phase : Phase, terminal phase = true → tick phase = phase

theorem terminalStable_true : terminalStable := by
  intro phase h
  cases phase <;> simp [Hidden.Workflow.terminal, Hidden.Workflow.tick] at h ⊢

def sampleRoute : List Phase := [queued, running, done]

theorem sampleRoute_length : sampleRoute.length = 3 := by
  rfl

end Hidden.Workflow.Target
