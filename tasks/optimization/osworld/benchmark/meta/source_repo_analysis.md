# Source Analysis

- Source: https://github.com/xlang-ai/OSWorld
- Pinned commit: `091f5ef1d5544bc74953c77875d5feb5bed30108`
- License: Apache-2.0

## Overview

OSWorld evaluates multimodal agents in real desktop applications using screenshots, keyboard/mouse actions, VM snapshots and official validators.

## Runtime and outputs

The final observable artifact is the validated VM state plus authoritative action trace. The Builder only receives task instructions and a live-agent interface.

## Benchmark boundary

The benchmark evaluates the created harness's final observable predictions or environment outcome, not reproduction of the source repository. Excluded from Builder-visible inputs: source prompts, gold labels, reference trajectories, evaluator code, hidden split metadata, and credentials.

Excluded/unstable portions: Tasks requiring external accounts or unstable web state are not selected in this compact split; VM credentials and validator details remain evaluator-owned.
