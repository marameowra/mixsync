# `tests/unit`

Fast tests with no network and no containers. Runs on every push.

## Layout
Mirrors the package: `tests/unit/<module>/test_*.py` for each folder under `src/mixsync/` (`core/`, `db/`, `match/`, `library/`, …).

## What belongs here
- Pure logic: `match` features and banding, `paths` templating (Hypothesis), the job state machine, the canonical policy.
- Adapters against **cassettes** (`tests/cassettes/`) or recorded JSON (`tests/fixtures/`).
- Services against **fakes** of the `core` Protocols.
- `db` against SQLite in-memory (and Postgres when `--db=postgres`).
- Crash-safety tests (`-m crash`) that spawn a subprocess and SIGKILL it at instrumented points.

## Matcher regression suite
`tests/unit/match/test_regression.py` scores the labeled set in `tests/fixtures/` and fails below the stored baseline (`tests/fixtures/regression_baseline.json`). Raising the baseline is a deliberate, reviewed commit.

## Design docs
[Testing](../../docs/testing.md)
