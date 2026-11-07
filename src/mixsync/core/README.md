# `core`

## Purpose
The shared vocabulary: domain types, Protocols, the job state machine, config and capabilities. Everything depends on `core`; `core` depends on nothing internal.

## Owns / does not own
- **Owns:** type definitions, the Protocols adapters implement (`DownloadSource`, `MetadataProvider`, `LibraryTarget`, `HistorySource`), enums, job-transition rules, the `Settings` schema, the clock, the error hierarchy.
- **Does not own:** persistence (`db`), HTTP, file I/O, or any third-party API.

## Rules / invariants
- Domain types are frozen pydantic models or dataclasses. No ORM objects leak out of `db`.
- `jobs.py` is the only place that defines legal job transitions. `db` and `worker` call into it; they never set `status` by hand.
- `Settings` validates at startup; missing required config fails fast with a readable message.
- Time comes from the injected `Clock`, so tests can use a fake.
- Errors decide retry behaviour: `TransientError` retries, `PermanentError` fails, `SafetyError` stops and alerts.

## May import
Nothing internal (pydantic, pydantic-settings and the stdlib only).

## Design doc
[Architecture: users and permissions](../../../docs/architecture.md#users-and-permissions) · [ADR 0004](../../../docs/decisions/0004-sqlite-and-db-job-queue.md)
