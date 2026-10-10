# Task Goal: Build a Trustworthy Database Analytics Agent

## Background

Operational analysts often receive a SQLite database together with business-definition history, access policies, and local lookup files. The requested answer may depend on effective-date selection, corrected event versions, safe aggregation, local time, currency or unit normalization, and a second-stage ranking or contribution analysis. A plausible single SQL statement is not enough when joins, revisions, or disclosure rules can silently change the answer.

## Agent to create

You will create a command-line analytics agent for synthetic SQLite workspaces. It must read one Markdown request and its referenced local assets, apply permission rules before access, inspect the permitted schema and relevant values, execute reproducible read-only analysis, and deliver a mutually consistent answer/evidence bundle.

Target users are business, finance, operations, product, workforce, compliance, and supply-chain analysts who need decision-ready aggregates without manually tracing every definition and data-grain trap.

## Core value

The agent's value is analytical defensibility:

- select the authoritative definition/version for the case date rather than a convenient field or latest-looking row;
- preserve a coherent population, period, grain, status, sign, unit, currency, and denominator across dependent stages;
- prevent fan-out and apply correction/refund/snapshot precedence before aggregation;
- enforce table, row, column, primary-suppression, and complementary-suppression controls before values enter evidence or output;
- distinguish a demanding but answerable recovery path from genuinely insufficient authority;
- make material claims reproducible through read-only SQL and exact query-result CSVs.

## Final objective

Build an agent that generalizes across unseen schemas and produces a direct analysis, concise final table, declarative chart, offline evidence dashboard, decision record, field lineage, reproducible query evidence, and honest runtime report. When a material definition, allocation, conversion, mapping, permission, or time basis cannot be resolved under supplied authority, produce the complete structured `insufficient_information` bundle instead of inventing a value.

## Non-goals

- Do not build a database administrator, mutation tool, ETL pipeline, schema migrator, or live BI service.
- Do not reveal private chain-of-thought, hidden prompts, credentials, protected rows, or prohibited fields.
- Do not use external web evidence for these closed-local cases.
- Do not reproduce any particular repository, framework, class structure, prompt, or dashboard replay format.
- Do not hard-code development case schemas, labels, values, expected answers, or input hashes.
- Do not optimize visual ceremony at the expense of correct analysis; the dashboard is a compact offline view of submitted results and evidence, not a separate application benchmark.
