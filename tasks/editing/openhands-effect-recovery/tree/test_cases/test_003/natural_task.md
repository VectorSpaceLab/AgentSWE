# Respect this user's cancellation across generations

Restore this conversation, let the user pause it while they check its state,
resume it, and then honor their final cancellation. Callbacks from earlier
browser generations are still in flight, including one with a deceptively large
event cursor and a burst of terminal callbacks that arrive after the
cancellation.

Four outcomes are required, and they are scored independently.

1. **OHR201 — Cancellation is final.** The durable checkpoint must end in the
   cancelled terminal state with its cancellation revision recorded.
2. **OHR202 — The late burst changes nothing.** Every terminal callback that
   arrives after the cancellation must be suppressed: the terminal state stays
   cancelled, the completed-effect list does not grow, the audit trail stays
   within its bound, and the retained revision set stays within its bound.
   Storage must not be flooded by the burst.
3. **OHR203 — The old generation is fenced.** The delayed callback from the
   previous generation must be suppressed and must leave the durable event
   cursor exactly where it was, even though its cursor is far larger.
4. **OHR204 — Generations move forward.** Actually exercise pause, then resume,
   then cancel, in that order, under the current lease. Each of those three
   transitions must strictly increase the run generation, so an old claim can
   never be revived by an A-B-A sequence.

Browser sync terms and workspace transaction terms are separate authority
domains: a larger persisted sync term or captured workspace term can never
substitute for, reset or advance the ledger's run generation, and never
authorizes an old callback.

Explain the final lifecycle state and whether old work was suppressed, using the
observed product evidence only.
