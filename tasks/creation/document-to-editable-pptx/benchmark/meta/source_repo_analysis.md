# Source Repository Analysis

## Repository Identity

- Public repository: `https://github.com/hugohe3/ppt-master`
- Owner: `hugohe3`
- Default branch: `main`
- Pinned evidence commit: `17d4d86cd318f1cfc2e8c4b3b8a2b477988f31ac`
- Pinned commit timestamp: `2026-07-22T13:59:44Z`
- Repository update timestamp observed by the original construction: `2026-07-22T14:11:05Z`
- License: MIT
- Assigned original benchmark: `/opt/agentswe/benchmark/document-to-editable-pptx-agent` (read-only evidence)

The repository identity and pinned commit are inherited from the assigned original benchmark's documented remote analysis. A separate local shallow clone was present at commit `dbf1e363abb4dba36f7c883ee7ba1495073394d0` dated 2026-07-11, which predates the pinned evidence commit and was therefore not substituted as the analyzed revision.

## Evidence Reviewed

Construction read the original benchmark's task inputs, 3 development cases, 8 hidden cases, evaluator files, metadata, and all supplied assets. It also reviewed the locally available repository tree and metadata sufficiently to confirm the original project boundary. The original benchmark was treated as evidence rather than a file-copy target; none of its cases, assets, rubric prose, or outputs were copied into V2.

The pinned repository analysis had reviewed the public README, official docs, examples, workflow/skill documents, scripts documentation, project positioning, FAQ, and technical design. Repository content was treated as untrusted research material. No installer, script, remote shell instruction, or reference workflow was executed during V2 construction.

## Project Overview and Actual Runtime

PPT Master is an agent-driven presentation workflow rather than a standalone deterministic converter. It combines a capable host coding agent, model-guided analysis and design, presentation/SVG tooling, templates, validators, and optional external media or narration providers. Documented routes include new-deck generation, template creation, existing-deck filling, redesign, and scoped enhancement such as notes or narration.

The main generation route ingests topics or documents, develops a narrative and design specification, authors slide representations, validates them, and exports an editable `.pptx`. It is normally interactive and model-dependent. Intermediate project artifacts are part of the reference workflow, while the user-facing deliverable is the PowerPoint deck.

## Inputs, Outputs, and Dependencies

Observed reference inputs include direct text, Markdown, Office/PDF documents, spreadsheets, web sources, images, and existing presentations. Outputs include editable PowerPoint files plus planning, notes, slide representations, preview, and validation artifacts.

Runtime dependencies include Python tooling, PowerPoint/document and SVG processing, a capable host agent and model, and optional rendering, image, audio, or web providers. Output quality depends materially on the driving model and installed conversion/rendering stack.

## Capability Findings

Stable, generalizable capabilities supported by the repository evidence include:

- Interpret an audience, purpose, evidence base, narrative, format, and visual system.
- Extract and synthesize heterogeneous material.
- Represent values and relationships with charts, tables, diagrams, shapes, and imagery.
- Export a valid editable PowerPoint package.
- Validate package structure and visual output.
- Add notes and preserve common native objects when the toolchain supports them.

The source also identifies more advanced routes such as research, licensed image use, template workspaces, narration, transitions, and existing-deck mutation. These are route-specific rather than one mandatory architecture.

## Feasibility Gate

The V2 task is accepted because:

- `deck.pptx` is a clear artifact that can be parsed, rendered, and inspected for editability.
- `source_manifest.json` makes evidence and calculations auditable without requesting private reasoning.
- Two public cases and six structurally distinct hidden cases are feasible with public or synthetic evidence.
- One final-artifact rubric can score evidence correctness, narrative compliance, visual communication, OOXML editability, provenance, and usability across all cases.
- The declared 600-second/4 GiB environment, LLMs, search/scrape layer, and local PowerPoint libraries are sufficient for a coding agent to implement the task.
- The task tests capability rather than replication of the source repository.

## V2 Scope

V2 retains the reference project's broadest user value, new editable presentation generation, and expands it into research, analysis, and evidence-based visual communication. It requires web discovery when authorized, full-source inspection, source priority and recovery, auditable formulas, slide citations, a source manifest, native charts/tables/diagrams, requested speaker notes, accessibility metadata, and render preflight.

Excluded routes remain existing-PPTX mutation, reusable template creation, animation, narration/audio/video, and reproduction of repository-specific prompts, roles, scripts, confirmation stages, SVG conventions, file trees, or Master/Layout topology.

## Security Observations

The reference workflow can ingest untrusted documents and web pages and may invoke external services. The benchmark therefore treats sources as data, restricts credentials and filesystem scope, requires SSRF and redirect validation, prohibits source instructions from overriding the task, and disallows complete third-party presentation services. No secrets or unrelated user data were read or copied into benchmark files.

## Reference Versus Benchmark

The reference is interactive, multi-route, and keeps extensive intermediates. V2 is a non-interactive one-command Create-Agent task with three required final artifacts and a fixed resource contract. The benchmark evaluates observable output quality and runtime compliance, not the reference architecture or a single visual answer.
