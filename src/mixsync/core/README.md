# `core`

## Purpose
The shared vocabulary: domain types, interfaces (Protocols), the job state machine, config, and capabilities. Everything else depends on `core`; `core` depends on nothing internal.

## Owns / does not own
- **Owns:** type definitions, Protocols, enums, state-transition rules, config schema.
- **Does not own:** persistence (that's `db`), HTTP, file I/O, or any third-party API.

## Proposed files
| File | Contents |
|---|---|
| `types.py` | `MBID` (NewType over `UUID`), `RecordingRef`, `ReleaseRef`, `ReleaseGroupRef`, `ArtistCredit`, `AudioFileInfo` (path, size, duration, format, bitrate, hash) |
| `requests.py` | `Request`, `RequestSource` (`manual` / `watchlist` / `discovery` / `migration`), `RequestStatus` |
| `matching.py` | `Candidate`, `CandidateFile`, `Evidence`, `MatchFeatures`, `MatchResult`, `Band` (`auto_accept` / `review` / `reject`), `Profile` |
| `protocols.py` | `DownloadSource`, `MetadataProvider`, `LibraryTarget`, `HistorySource`, `Journal`, plus repository Protocols that `db` implements |
| `jobs.py` | `JobKind`, `JobState`, allowed transitions, retry/backoff policy |
| `capabilities.py` | `Capability` enum and default role bundles |
| `config.py` | `Settings` (pydantic-settings): env vars + optional `/config/mixsync.toml` |
| `clock.py` | `Clock` Protocol (`now()`) + system implementation; injected everywhere time matters so tests can use a fake clock |
| `errors.py` | Error hierarchy: `TransientError` (retry), `PermanentError` (fail), `SafetyError` (stop and alert) |

## Key interfaces
```
JobState:  queued → leased → running → succeeded
                              running → retry_wait → queued
                              running → failed
           (any non-terminal) → cancelled
Leases that expire return to queued (the reaper in worker.py does this).

Capability: request, download.auto, approve, library.edit, workflow.edit, admin
Default roles (v1): admin = all; user = request + download.auto

Journal (Protocol; implemented in db):
  begin(batch_id, kind, src, dst) -> op_id
  complete(op_id, dst_hash)
  fail(op_id, error)
  pending() -> list[op]        # used for startup recovery
```

## Rules
- Domain types are **frozen** pydantic models or dataclasses. No ORM objects leak out of `db`.
- The state machine is the only place that defines legal job transitions. `db` and `worker` call into it; they never set `status` by hand.
- `Settings` validates at startup. Missing required config fails fast with a readable message.

## May import from
Nothing internal. Third-party: pydantic, pydantic-settings, the stdlib.

## Tests
`tests/unit/core/`: state-machine transitions (legal and illegal), settings validation, `MBID` parsing.

## Design docs
[Architecture: users and permissions](../../../docs/architecture.md#users-and-permissions) · [ADR 0004](../../../docs/decisions/0004-sqlite-and-db-job-queue.md)
