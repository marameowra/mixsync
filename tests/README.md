# Tests

Strategy lives in [docs/testing.md](../docs/testing.md). This folder is where the tests themselves live.

## Layout
| Folder | What | Runs |
|---|---|---|
| [unit/](unit/README.md) | Fast, no network, no containers | every push |
| [integration/](integration/README.md) | Real slskd + Navidrome + MixSync in compose | nightly + on demand |
| [fixtures/](fixtures/README.md) | Audio clips, recorded search results, labeled match set | used by both |
| [cassettes/](cassettes/README.md) | vcrpy HTTP recordings for external APIs | used by unit |

## Commands (once the project exists)
```
uv run pytest                      # unit + regression suite
uv run pytest -m integration       # needs docker compose
uv run pytest -m crash             # SIGKILL crash-safety tests
uv run pytest --db=postgres        # run against Postgres instead of SQLite
```

## Rules
- Unit tests never touch the network. A pytest fixture blocks sockets; cassettes or fakes only.
- Every service test uses **fakes for the core Protocols**, not mocks of adapter internals. The fakes live in `tests/fakes/` (create it with the first service test).
- Time is injected (`Clock` from `core`), so retention, watchlist, and backoff tests use a fake clock.
- Filesystem tests use `tmp_path` with a `/data`-shaped tree; never the real library.
