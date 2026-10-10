# Resources

The evaluator locks the vision model to `gpt-5.6-sol` with `high` reasoning. Real credentials are
available only to an evaluator-owned broker; neither Builder nor submitted policy receives them.
The broker accepts only evaluator screenshots and the strict single-action schema. It rejects
remote image URLs, provider tools, file IDs, streaming, storage, and candidate-selected request
fields.

Each case is limited to 30 GUI actions, 30 model calls, 100,000 model tokens, and 900 seconds of
agent execution after VM boot and official task setup. VM, task setup, network preflight, fixture,
model transport, official evaluator, or cleanup failure is infrastructure and does not consume a
valid development round. Invalid policy, malformed action, wrong GUI work, agent timeout, and
action/model budget exhaustion are ordinary official outcomes.

The Builder has no Docker socket, VM image, hidden task metadata, evaluator implementation,
credentials, other submissions, or runtime traces. Public internet available during Builder work
does not grant access to those evaluator-owned resources.
