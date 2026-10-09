# ADR 0004: SQLite and a DB-backed job queue

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
MixSync runs on a home server with Docker Compose. It needs durable state (requests, matches, journal, users) and durable background jobs (downloads, imports, polling). Hosting should stay flexible: one container for small installs, separate workers for larger ones.

## Decision
- **SQLAlchemy 2 + Alembic.** **SQLite in WAL mode** by default; Postgres supported through configuration.
- **The job queue is a table in the same database.** Workers claim jobs with a lease (`locked_by`, `lease_expires_at`). Expired leases are reclaimed. Every job is idempotent and resumable.
- Migrations are mandatory for every schema change.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **SQLite + DB job table (chosen)** | Zero extra services; jobs and state commit in one transaction; trivial backup (`VACUUM INTO`) | SQLite has a single writer, which is fine at home scale; the queue code must be written carefully |
| Postgres + Procrastinate | Mature queue, LISTEN/NOTIFY | Requires Postgres for even the smallest install |
| Redis + Dramatiq / arq / Celery | Mature, fast | An extra service; job state is separate from DB state, so crash consistency is harder |
| Huey (SQLite backend) | Small, simple | A separate store from the app DB; less control over leases |

## Consequences
- One container (web + worker) plus a SQLite file is a complete install.
- Moving to Postgres is a config change plus a data copy, not a code change. CI runs the test suite on both databases.
- A job's state change and its domain change commit together, which is the basis for [data safety](../design/data-safety.md).
