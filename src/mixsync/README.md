# `mixsync` package

The Python package. Every subfolder has a README; read it before adding code to a module. The import rules below are enforced by `uv run lint-imports` (contracts in `pyproject.toml`).

## Modules
| Module | Kind | One line |
|---|---|---|
| [core](core/README.md) | foundation | Domain types, Protocols, job state machine, config, capabilities |
| [db](db/README.md) | foundation | SQLAlchemy models, Alembic migrations, queue, journal, secret box |
| [ratelimit](ratelimit/README.md) | foundation | Per-service token buckets, HTTP cache, User-Agent |
| [sources](sources/README.md) | adapter | `DownloadSource` implementations (slskd) |
| [metadata](metadata/README.md) | adapter | MusicBrainz, AcoustID, Cover Art Archive clients |
| [targets](targets/README.md) | adapter | `LibraryTarget` implementations (Navidrome) |
| [match](match/README.md) | logic | Feature extraction, scoring, bands, vetoes |
| [library](library/README.md) | logic | Paths, safe file ops, tags, canonical releases, importer |
| [acquire](acquire/README.md) | service | The request pipeline, merge, migration, drop folder, re-check |
| [discovery](discovery/README.md) | adapter + service | ListenBrainz adapter, playlist generator, retention |
| [watchlist](watchlist/README.md) | service | Follows and the release poller |
| [submit](submit/README.md) | service | MusicBrainz seeding and Harmony links |
| [web](web/README.md) | composition root | FastAPI app, auth, server-rendered UI |

## Entry points (top-level files)
- `app.py`: builds the FastAPI app from a `Services`. `mixsync web`.
- `wiring.py`: composition root for what the worker and web share (engine, `FileOps`, the one `MusicBrainzProvider`, `Merger`, HTTP clients, `SecretBox`). `build_services(settings)` only constructs: no recovery, no startup chains. A bad key file fails here.
- `worker.py`: the job loop. Claims jobs, dispatches by `JobKind`, heartbeats leases, runs startup journal recovery ([data safety](../../docs/design/data-safety.md#4-operation-journal)). Recurring chain initializers run in worker threads before polling and retry at the polling interval; discovery and retention initialize together. `mixsync worker`.
- `__main__.py`: `mixsync web | worker | all | migrate | user-add | backup`. `all` builds one `Services` for both.
  > Planned — not built (#222): `mixsync backup` only prints "not implemented yet".

## Dependency rules
Arrows mean "may import from". Anything not listed is forbidden.

```
core        ← nothing internal
db          ← core
ratelimit   ← core, db
adapters    ← core, ratelimit        sources, metadata, targets, discovery.listenbrainz
match       ← core
library     ← core, db, match
acquire     ← core, db, match, library
services    ← core, db, match, library   submit, discovery.playlists, discovery.retention
watchlist   ← core, db, match, library, acquire
web, worker, wiring, app ← anything   composition roots
```

- **Adapters never import `db`.** They take and return `core` types.
- **Services never import adapters.** They use the `core` Protocols (`DownloadSource`, `MetadataProvider`, `LibraryTarget`, `HistorySource`); `app.py`, `worker.py` and `wiring.py` inject the concrete adapters.
- **No domain package imports `wiring`.**
- **`match` is pure.** No I/O: callers gather the evidence and pass it in.

## Cross-cutting rules
- All outbound HTTP goes through `ratelimit`. No raw `httpx.AsyncClient()` elsewhere.
- All filesystem writes under `/data` go through `library/fileops.py`.
- pyright strict and ruff must pass. No `# type: ignore` without a comment saying why.
- Async wherever I/O happens. CPU-bound work (fingerprinting, hashing) runs in a thread or process pool.

## Design docs
[Architecture](../../docs/architecture.md) · [Roadmap](../../docs/roadmap.md) · [Testing](../../docs/testing.md)
