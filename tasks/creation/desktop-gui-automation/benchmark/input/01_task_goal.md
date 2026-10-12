# Task Goal

## Background

Important operational work rarely fits in one form. A person must inspect unfamiliar screens, carry facts across pages, interpret visual layouts, recover from validation failures, preserve unrelated records, and verify the exact boundary where a synthetic transaction becomes committed. Each benchmark case provides a safe local application with no real account, payment, message, or production effect.

## Agent to Create

You must create a **Stateful Local GUI Workflow Agent**. It receives a Markdown request and the URL of a harness-started local web application, performs the requested workflow through the rendered interface, and returns auditable visual and structured evidence.

## Target Users and Core Value

The target users are operations teams, QA engineers, accessibility testers, schedulers, and knowledge workers. Your agent's value is reliable completion of multi-screen workflows while preserving exact constraints and unrelated application state.

Your agent must observe before acting; navigate tabs, drawers, dialogs, paginated or virtualized collections, and responsive layouts; use pointer coordinates for spatial controls; track selected records and prerequisites; recognize asynchronous completion; recover from stale state and validation errors; stop at review boundaries when instructed; and verify visible results after consequential actions.

## Final Objective

For every valid case, reach the requested state in the active fixture and produce `automation_result.json`, three phase-specific screenshots, and `run_report.json`. The same implementation must generalize across the two public development cases and six structurally different hidden cases.

## Non-Goals

You do not control the host desktop, real accounts, production services, remote browsers, payments, messages, or arbitrary websites. Do not recreate or modify fixture source, call application internals, directly mutate state, inspect the evaluator bridge, or expose private reasoning. Pixel-only control is not mandatory: normal accessibility and DOM metadata for rendered controls are allowed, but cases include decisive information available only in rendered image, canvas, or spatial layout content.
