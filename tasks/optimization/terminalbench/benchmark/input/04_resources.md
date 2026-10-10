# Resources

Docker and Docker Compose are evaluator-owned and are not exposed to the submitted harness. The harness emits a live Terminus-2 policy specification; the evaluator-owned runtime calls the locked `gpt-5.6-sol` model through a credential broker and executes commands only inside the official task container. Per row budget is 900 seconds, at most 20 model calls/turns and 200,000 broker-counted tokens. The Candidate does not receive a real credential, provider URL, hidden task metadata or tests.

Credentials are mounted only into the evaluator-owned broker. The public development package contains no secret values. Never put credentials in predictions, reports, command text, URLs, or logs. The evaluator records model calls, terminal turns, wall time and failures.
