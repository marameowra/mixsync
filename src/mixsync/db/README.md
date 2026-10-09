# `db`: proposed database implementation

## Purpose
Persistent state for MixSync: models, migrations, the repository implementations of the `core` Protocols, the job queue, and the operation journal.

## Owns / does not own
- **Owns:** SQLAlchemy models, Alembic migrations, sessions/engine setup, repositories, job claim and lease SQL, backups.
- **Does not own:** business rules. Repositories store and fetch; they don't decide.

## Proposed files
| File | Contents |
|---|---|
| `engine.py` | Engine/session factory, SQLite pragmas, Postgres options |
| `base.py` | `DeclarativeBase` with a `MetaData` naming convention, plus mixins (`TimestampMixin`) |
| `models/` | One file per table group (below): `auth.py`, `work.py`, `acquisition.py`, `matching.py`, `library.py`, `safety.py`, `features.py`, `infra.py` |
| `repos/` | Repository classes implementing `core.protocols` |
| `queue.py` | Job enqueue, claim, heartbeat, complete, and reap |
| `journal.py` | `Journal` implementation over `file_ops` |
| `backup.py` | `VACUUM INTO` (SQLite) / `pg_dump` wrapper and rotation |
| `migrations/` | Alembic environment + versions |

## Conventions
- **SQLAlchemy 2 typed ORM** (`Mapped[]`, `mapped_column`) + **Alembic**. One migration per schema change; every migration has a working `downgrade`.
- **Naming convention** on `MetaData` (`ix_`, `uq_`, `fk_`, `ck_`, `pk_`), so constraint names are stable across databases and Alembic autogenerate works.
- **Postgres-compatible only.** No SQLite-only features in models. CI runs the suite on both databases.
- **SQLite pragmas** on every connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`. Writes that claim jobs use `BEGIN IMMEDIATE`.
- **Keys:** integer surrogate PKs. MBIDs are stored as `Uuid` columns, unique where they identify a row.
- **Time:** UTC everywhere, `DateTime(timezone=True)`. Every table has `created_at` / `updated_at`.
- **No deletes for domain rows.** Use a `status` enum. Hard delete only for ephemeral rows (expired sessions, purged cache).
- **JSON columns** only for opaque payloads you never filter on: feature vectors, raw API snapshots, tag snapshots.
- **Secrets** (user API tokens) are encrypted at rest with Fernet (`cryptography`).

> [!question] Encryption key source
> Choose where the Fernet key lives: env var `MIXSYNC_SECRET_KEY`, or a generated `/config/secret.key` (chmod 600). Proposal: support both, with the env var taking precedence. If the key is lost, linked accounts must be re-entered; nothing else is affected.

## Tables

### Users & auth
| Table | Key columns |
|---|---|
| `users` | id, username (unique), password_hash (argon2), display_name, status (`active`/`disabled`), matching_profile_id → |
| `roles` | id, name (unique) |
| `role_capabilities` | role_id →, capability (enum) · PK(role_id, capability) |
| `user_roles` | user_id →, role_id → · PK(user_id, role_id) |
| `sessions` | id (random token hash), user_id →, expires_at, last_seen_at, user_agent |
| `user_links` | id, user_id →, service (`navidrome`/`listenbrainz`/`lastfm`/`acoustid`), external_username, secret_encrypted · UQ(user_id, service) |
| `matching_profiles` | id, name, preset (`strict`/`balanced`/`loose`/`custom`), auto_accept_max, review_max, quality_pref (JSON) |

### Work
| Table | Key columns |
|---|---|
| `requests` | id, user_id →, source (enum), kind (`artist`/`release_group`/`release`/`recording`), target_mbid, provisional (bool), status, follow_id → (nullable), notes |
| `jobs` | id, kind (enum), payload (JSON), status (enum), priority, attempts, max_attempts, run_after, locked_by, lease_expires_at, last_error, idempotency_key (unique, nullable), request_id → (nullable), parent_job_id → (nullable) |

### Acquisition
| Table | Key columns |
|---|---|
| `search_runs` | id, request_id →, source (`slskd`), query, started_at, finished_at, result_count, status |
| `candidates` | id, search_run_id →, peer, folder, files (JSON), features (JSON), distance, rank, chosen (bool) |
| `downloads` | id, candidate_id →, source_transfer_ids (JSON), local_paths (JSON), state, bytes_done, bytes_total |

### Matching
| Table | Key columns |
|---|---|
| `match_decisions` | id, download_id →, file_path, candidate_recording_mbid, candidate_release_mbid, features (JSON), distance, band, scorer_version, vetoes (JSON), user_action (`accept`/`pick_other`/`mark_unverified`/`reject`, nullable), decided_by → (nullable), decided_at |

This table is the calibration dataset ([matching](../../../docs/design/matching.md#calibration)). Never prune it.

### Library
| Table | Key columns |
|---|---|
| `release_groups` | id, mbid (unique), title, primary_type, first_release_date |
| `releases` | id, mbid (unique), release_group_id →, title, country, date, media_format, track_count, status |
| `canonical_releases` | release_group_id → (PK), release_id →, policy_snapshot (JSON), chosen_at, chosen_by (`policy`/user id) |
| `tracks` | id, library (`main`/`discover`), path (relative to library root; UQ(library, path)), status (`verified`/`unverified`), recording_mbid, release_id →, track_mbid, disc, position, title, artist_credit, duration_ms, size_bytes, hash, format, bitrate, acoustid_id, is_bonus, request_id → |

Indexes: `tracks(recording_mbid)`, `tracks(release_id)`, `tracks(status)`.

> [!question] How much MusicBrainz data to cache
> Option A: store only ids + display fields in `releases`/`release_groups` and rely on the HTTP cache for everything else (the proposal). Option B: store full MB entity JSON. B makes offline browsing richer but duplicates the cache and goes stale.

### Safety
| Table | Key columns |
|---|---|
| `file_ops` | id (op_id), batch_id, kind (`copy`/`move`/`trash`/`retag`/`purge`), src, dst, src_hash, dst_hash, status (`started`/`done`/`failed`/`rolled_back`), started_at, finished_at, error |
| `tag_snapshots` | id, track_id → (nullable), file_path, op_id →, tags (JSON), taken_at |

Index: `file_ops(status)` for startup recovery and `file_ops(batch_id)` for undo.

### Features
| Table | Key columns |
|---|---|
| `follows` | id, user_id →, entity (`artist`/`label`), mbid, filters (JSON), weekly_cap, last_checked_at, next_check_at |
| `follow_hits` | id, follow_id →, release_group_mbid, request_id → (nullable), action (`requested`/`skipped_owned`/`skipped_cap`) · UQ(follow_id, release_group_mbid) |
| `playlists` | id, user_id →, name, definition (JSON: sources, size, unowned_ratio, cadence), target_playlist_id (Navidrome id), last_built_at |
| `playlist_items` | playlist_id →, position, recording_mbid, track_id → (nullable until owned) · PK(playlist_id, position) |
| `provisional_tracks` | id, track_id →, user_id →, expires_at, kept_at (nullable), keep_reason (`star`/`plays`/`manual`), expired_at (nullable) · UQ(track_id, user_id) |

### Infra
| Table | Key columns |
|---|---|
| `rate_buckets` | service (PK), tokens, capacity, refill_per_sec, updated_at, blocked_until |
| `http_cache` | key (PK), service, status, headers (JSON), body, fetched_at, expires_at (only if hishel's own storage is not used) |
| `settings` | key (PK), value (JSON), updated_at, updated_by → |

## Job queue
**Claim** (one statement, works on both DBs; SQLite ≥ 3.35 for `RETURNING`):
```sql
UPDATE jobs
SET status = 'leased', locked_by = :worker, lease_expires_at = :now + :lease, attempts = attempts + 1
WHERE id = (
  SELECT id FROM jobs
  WHERE (status = 'queued' AND run_after <= :now)
     OR (status = 'leased' AND lease_expires_at < :now)
  ORDER BY priority, id
  LIMIT 1
  -- Postgres: FOR UPDATE SKIP LOCKED
)
RETURNING *;
```
- **Heartbeat:** the worker extends `lease_expires_at` every lease/3 while running.
- **Reaper:** re-queues expired `running` jobs. The handler must be idempotent, because a job can run twice.
- **Domain change + job transition commit in one transaction.**
- **Idempotency:** `idempotency_key` (e.g. `import:{download_id}`) stops duplicate enqueues.

## May import from
`core` only (plus SQLAlchemy, Alembic, cryptography).

## Tests
- `tests/unit/db/`: repositories against in-memory SQLite. Claim races: two connections, one job, exactly one winner.
- Migration test: upgrade → downgrade → upgrade on an empty DB, for both SQLite and Postgres.
- Crash test: kill mid-transaction → no half-written job or domain state.

## Design docs
[ADR 0004](../../../docs/decisions/0004-sqlite-and-db-job-queue.md) · [Data safety](../../../docs/design/data-safety.md) · [Matching](../../../docs/design/matching.md)
