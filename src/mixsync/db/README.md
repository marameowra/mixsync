# `db`

## Purpose
Persistent state: SQLAlchemy models, Alembic migrations, the job queue, the operation journal, and the encryption box for stored secrets.

## Owns / does not own
- **Owns:** `models/`, `migrations/`, engine and session setup (`engine.py`), `queue.py` (enqueue, claim, heartbeat, reap), `journal.py`, `undo.py`, `secretbox.py`, and the small query modules (`auth.py`, `canonical.py`, `decisions.py`).
- **Does not own:** business rules. This package stores and fetches; it does not decide.

## Rules / invariants
- SQLAlchemy 2 typed ORM plus Alembic. One migration per schema change, each with a working `downgrade`; the `MetaData` naming convention keeps constraint names stable.
- Postgres-compatible only: no SQLite-only features in models. CI runs the suite on both (see [tests/README.md](../../../tests/README.md)).
- SQLite connections set WAL, foreign keys, a busy timeout and `synchronous=NORMAL`; job-claiming writes use `BEGIN IMMEDIATE`.
- UTC everywhere (`DateTime(timezone=True)`). MBIDs are `Uuid` columns.
- No deletes for domain rows: use a `status`. Hard delete only for ephemeral rows (expired sessions, purged cache).
- JSON columns only for opaque payloads that are never filtered on.
- A job's state change and its domain change commit in one transaction; handlers must be idempotent, because a job can run twice. `idempotency_key` stops duplicate enqueues.
- `match_decisions` is the calibration dataset ([matching](../../../docs/design/matching.md#calibration)). Never prune it.
- MusicBrainz data: ids plus filterable fields only ([ADR 0011](../../../docs/decisions/0011-musicbrainz-ids-and-key-fields-only.md)).
- Secrets are Fernet-encrypted with an operator-supplied key ([ADR 0010](../../../docs/decisions/0010-secret-key-source.md)).
  > Planned — not built (#222): database backups.

## May import
`core` only (plus SQLAlchemy, Alembic, cryptography).

## Design doc
[ADR 0004](../../../docs/decisions/0004-sqlite-and-db-job-queue.md) · [Data safety](../../../docs/design/data-safety.md) · [Matching](../../../docs/design/matching.md)
