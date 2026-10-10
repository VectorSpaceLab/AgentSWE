# Reference Baseline

## Status

The pinned reference implementation was not run end to end against v4 and produced no benchmark output. V4 has no stored reference answer or candidate output.

## Inherited static evidence

The predecessor analysis inspected the pinned source archive at `365d0617c1a4567ffee1b19b40c27feb4206bfcf`. Two local reference-runtime attempts did not reach benchmark execution: selected tests could not start because `pytest` was absent from the base environment, and a source-tree import stopped at the undeclared local availability of `pandas`. No credentials were loaded, no database service was used, and no benchmark case was passed to the reference.

Static inspection established the presence of agent entry points, SQLite runner integration, SQL execution and CSV-result tooling, visualization support, user context, and permission transformation tests. That evidence supports feasibility but not v4 correctness.

## Why v4 uses no reference output

The benchmark is an implementation-independent create task whose correct outputs come from the supplied synthetic assets and public definitions. Configuring the reference's provider/server stack would not produce a canonical answer to v4's stricter correction, privacy, effective-date, recovery, and insufficiency contracts. The benchmark fixtures and evaluator checks are directly reproducible with SQLite and the Python standard library.

## Limitations

- No reference accuracy, timing, memory, provider-count, dashboard, or privacy score is claimed.
- Static evidence does not prove the reference would satisfy v4.
- No reference output, query, answer, or hidden expected-value file exists in the released benchmark.
