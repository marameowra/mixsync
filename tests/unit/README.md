# `tests/unit`

Fast tests with no network and no containers. Runs on every push.

## Layout
Mirrors the package: `tests/unit/<module>/test_*.py` for each folder under `src/mixsync/` (`core/`, `db/`, `match/`, `library/`, …).

## What belongs here
- Pure logic: `match` features and banding, `paths` templating (Hypothesis), the job state machine, the canonical policy.
- Adapters against **cassettes** (`tests/cassettes/`) or recorded JSON (`tests/fixtures/`).
- Services against **fakes** of the `core` Protocols.
- `db` against SQLite (a temp file per test), and Postgres when `--db=postgres` (a schema per test; see [tests/README.md](../README.md)).
- Crash-safety tests (`-m crash`) that spawn a subprocess and SIGKILL it at instrumented points.

## Design docs
[Testing](../../docs/testing.md)
