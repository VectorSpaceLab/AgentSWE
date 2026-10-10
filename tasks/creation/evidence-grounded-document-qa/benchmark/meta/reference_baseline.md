# Reference Baseline

The upstream reference implementation was not executed. Its own isolated dependency environment and credentials were not provisioned for this construction, and reproducing its workflow is not required by the abstracted task.

Feasibility evidence instead comes from deterministic construction and parser/harness checks on the v4 artifacts. `meta/generate_assets.py` uses only the Python standard library and creates all PDF, DOCX, HTML, CSV, XLSX, SVG, and PNG fixtures. The evaluator parses those assets independently, checks locators against source-native content, validates exact bytes/hashes and claim/manifest linkage, and provides an offline Chromium interaction harness.

No generated answer is normative. Evaluators reconstruct each active case from its runtime request/assets and apply the global final-artifact rubric. The dev-only contract fixture under `evaluator/tests/` exists solely to regression-test schema, byte-bundle, linkage, and viewer contracts; it is not a case answer and contains no hidden facts.

Local construction environment limitation: no compatible Chromium-family executable was present, so browser interaction can only be recorded as executed if a later environment actually runs `validate_viewer_interaction.mjs`. Static and Node syntax checks do not count as a browser interaction pass.
