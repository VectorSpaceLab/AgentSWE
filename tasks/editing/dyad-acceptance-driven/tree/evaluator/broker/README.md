# Candidate-only broker boundary

`candidate_broker.py` is evaluator-owned. It loads `DEEPSEEK_API_KEY` only in the
broker process, rewrites every Responses request to the locked
`deepseek-flash`/`medium` protocol, and returns only provider output. The
Candidate receives a placeholder token and never receives this secret.

The broker accepts both JSON and streaming Responses responses. Stats are
monotonic and contain calls, failures, successful calls, and token counters;
they contain no prompts, credentials, oracle values, or raw provider output.
