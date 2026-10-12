# Resources

The evaluator runs the submitted specification with pinned OpenClaw `2026.7.1-2` and evaluator-owned model `gpt-5.6-sol` through a credential broker. The agent never receives the real API key. Local file and command tools are available with the `coding` tool profile; runtime public network is available for task-relevant public resources such as an explicitly named open-source repository. No Docker socket, evaluator source, hidden task files, other submission, or real credential is mounted.

Budget per row: at most 20 model calls, 600 seconds, and 200,000 model tokens. OpenClaw tools and provider usage are measured from evaluator-owned transcript and broker counters. Automated and hybrid scoring use the pinned PinchBench `grade_task()` implementation; infrastructure/provider failures do not become ordinary agent zeroes.

The Builder may use its separately authorized development resources while constructing the harness. Candidate predictions must not contain credentials, provider endpoints, hidden data, or grader material.
