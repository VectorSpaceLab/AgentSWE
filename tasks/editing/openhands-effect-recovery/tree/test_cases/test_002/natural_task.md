# Finish an interrupted workspace transfer without repeating any of it

Resume this OpenHands conversation's interrupted content-addressed workspace
transfer. One chunk's write response was lost after the remote side had already
persisted it, the remote commit response was lost after durable publication, and
the reconciler is being crashed at named boundaries.

Five outcomes are required, and they are scored independently.

1. **OHR101 — Upload exactly the missing chunk, once.** Re-probe what the remote
   side is genuinely missing instead of re-sending a chunk whose response was
   lost after it landed.
2. **OHR102 — Reach every injected crash boundary honestly.** Every boundary in
   the injected matrix must be reached with the state it names already durably
   observable before the throw, and the thrown `Error.message` must contain the
   boundary code verbatim. A fresh reconciler must then resume from the durable
   record rather than starting over.
3. **OHR103 — Reconcile, do not re-commit.** Resolve the lost commit response
   through the reconcile-commit path: exactly one durable remote commit, exactly
   one local apply, replicas converged on the union of the one-sided paths.
4. **OHR104 — Make the completion survive the browser.** Restart the product
   against the same durable storage and inspect again: the workspace record must
   read completed with the remote-commit, local-apply and cursor stages all
   committed. A completion that only exists in memory is not a completion.
5. **OHR105 — Keep runtime-event discipline.** See the paragraph below.

**Runtime-event discipline (measured in every case that names it).** Between
your actions this conversation receives real runtime traffic that you did not
send. A delivery that carries the current owner, lease epoch, fencing token,
run generation and expected revision is genuine: accept it and move the durable
`eventCursor` to exactly the cursor it carries. The same `eventId` delivered
again must change nothing. A delivery whose fencing token or lease epoch does
not match the live authority must be suppressed with the durable cursor
bit-identical, however large the cursor it claims. Suppressing everything is not
a safe answer: the genuine delivery must land. Read the current revision back
from the product before every authority-bearing call, because this traffic moves
it.

Keep every visible projection bounded: counts and phase codes only, never paths,
file contents, chunk or content digests, tab or incarnation identities, fencing
tokens or grant tokens.

Describe the observed result and any remaining block. Claiming an outcome the
product did not reach scores worse than reporting it as unmet.
