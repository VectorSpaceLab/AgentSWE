# Hidden scenario: pause/resume/cancel ABA prevention

Pause and resume while an effect callback is in flight, then settle the old
generation late. Separately cancel a newer live delivery and attempt to
resume. Attempt an old-generation retry/resume loop after a pause and flood
the cancelled state with late terminal events. Generations must prevent ABA,
cancel must remain terminal, and the audit/revision storage must stay
bounded. The browser sync term is a separate authority domain: even a larger
durable sync term cannot substitute for, reset, or advance the ledger run
generation. A workspace transaction carrying a larger sync term is likewise
orthogonal: its captured term cannot authorize an old ledger generation or a
recovery callback.
