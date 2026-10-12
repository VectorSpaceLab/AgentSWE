# Source Analysis

- Source: https://github.com/openai/simple-evals
- Pinned commit: `652c89d0ca9df547706735883097e9537d40dc47`
- License: MIT

## Overview

OpenAI simple-evals BrowseComp encrypted question/answer rows with the official Explanation/Exact Answer/Confidence contract.

## Runtime and outputs

Input is JSONL and output is JSONL. The final observable artifact is a response row; gold answers are decrypted only inside evaluator code.

## Benchmark boundary

The benchmark evaluates the created harness's final observable predictions or environment outcome, not reproduction of the source repository. Excluded from Builder-visible inputs: source prompts, gold labels, reference trajectories, evaluator code, hidden split metadata, and credentials.

Excluded/unstable portions: The full online test set and canary/decryption mechanism are not Builder resources; only a small frozen row split is used.
