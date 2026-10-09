# `mixsync` package

The Python package. Every subfolder has a README describing what goes there. Read the README before adding code to a module.

## Modules
| Module | Kind | One line |
|---|---|---|
| [core](core/README.md) | foundation | Domain types, Protocols, job state machine, config, capabilities |
| [db](db/README.md) | foundation | SQLAlchemy models, Alembic migrations, repository implementations |
| [ratelimit](ratelimit/README.md) | foundation | Per-service token buckets, HTTP cache, User-Agent |
| [sources](sources/README.md) | adapter | `DownloadSource` implementations (slskd) |
| [metadata](metadata/README.md) | adapter | `MetadataProvider` implementations (MusicBrainz, AcoustID, CAA) |
| [targets](targets/README.md) | adapter | `LibraryTarget` implementations (Navidrome) |
| [match](match/README.md) | logic | Feature extraction, scoring, bands, calibration |
| [library](library/README.md) | logic | Paths, safe file ops, tags, canonical releases, importer |
| [discovery](discovery/README.md) | adapter + service | ListenBrainz/Last.fm adapters, playlist generator, retention |
| [watchlist](watchlist/README.md) | service | Follows and the release poller |
| [submit](submit/README.md) | service | MusicBrainz seeding, Harmony links, unverified re-check |
| [web](web/README.md) | composition root | FastAPI app, auth, HTMX UI |

## Entry points (top-level files, not folders)
- `app.py`: builds the FastAPI app. Wires config → DB → adapters → services → routes. Run with `mixsync web`.
- `worker.py`: the job loop. Claims jobs from the DB queue, dispatches by `JobKind`, heartbeats leases, and runs the startup journal recovery ([data safety](../../docs/design/data-safety.md#4-operation-journal)). Run with `mixsync worker`.
- `__main__.py` / CLI: `mixsync web | worker | all | migrate | backup`.

## Dependency rules
Arrows mean "may import from". Anything not listed is forbidden. Enforce this in CI with `import-linter`.

```
core        ← nothing internal               domain types + Protocols
db          ← core
ratelimit   ← core, db
adapters    ← core, ratelimit                sources, metadata, targets, discovery adapters
match       ← core                           pure logic: features in, distance out
library     ← core, db, match
services    ← core, db, match, library       watchlist, submit, discovery services
web, worker ← anything                       composition roots
```

- **Adapters never import `db`.** They take and return `core` domain types.
- **Services never import adapter modules.** They depend on the `core` Protocols (`DownloadSource`, `MetadataProvider`, `LibraryTarget`, `HistorySource`), and `app.py` / `worker.py` inject the concrete adapters. This keeps the services testable with fakes and lets you swap sources and targets without touching services.
- **`match` is pure.** No I/O. Callers gather the evidence (fingerprints, MB data) and pass it in.

## Cross-cutting rules
- All outbound HTTP goes through `ratelimit`. No raw `httpx.AsyncClient()` anywhere else.
- All filesystem writes under `/data` go through `library/fileops.py`.
- pyright strict and ruff must pass. No `# type: ignore` without a comment explaining why.
- Async everywhere I/O happens. CPU-bound work (fingerprinting, hashing) runs in a thread or process pool.

## Design docs
[Architecture](../../docs/architecture.md) · [Roadmap](../../docs/roadmap.md) · [Testing](../../docs/testing.md)
