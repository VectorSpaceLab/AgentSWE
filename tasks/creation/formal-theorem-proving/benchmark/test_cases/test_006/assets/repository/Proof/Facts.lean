import Proof.State

namespace Hidden.Workflow.Facts

open Hidden.Workflow Hidden.Workflow.Phase

theorem queued_eligible : eligible queued := by
  trivial

theorem running_eligible : eligible running := by
  trivial

theorem idle_not_eligible : ¬ eligible idle := by
  intro h
  exact h

theorem done_not_eligible : ¬ eligible done := by
  intro h
  exact h

theorem tick_running_not_eligible : ¬ eligible (tick running) := by
  rw [tick_running]
  exact done_not_eligible

theorem queued_survives_tick : eligible (tick queued) := by
  rw [tick_queued]
  exact running_eligible

theorem done_fixed : tick done = done := by
  rfl

theorem remaining_running : remaining running = 1 := by
  rfl

theorem priority_increases_queued : priority queued < priority (tick queued) := by
  decide

theorem snapshot_done : snapshot done = (4, true) := by
  rfl

end Hidden.Workflow.Facts
