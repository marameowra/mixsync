# Tests

Strategy lives in [docs/testing.md](../docs/testing.md). This folder holds the tests themselves.

## Layout
| Folder | What | Runs |
|---|---|---|
| [unit/](unit/README.md) | Fast, no network, no containers | every push |
| [integration/](integration/README.md) | Real slskd + Navidrome + MixSync in compose | nightly + on demand |
| [fixtures/](fixtures/README.md) | Audio clips, recorded slskd results, labeled match set | both |
| [cassettes/](cassettes/README.md) | Recorded JSON bodies from external APIs | unit |

## Commands
```
uv run pytest                      # unit + regression suite
uv run pytest -m integration       # needs docker compose
uv run pytest -m crash             # SIGKILL crash-safety tests
uv run pytest --db=postgres        # against Postgres instead of SQLite
```
The unit suite needs `ffmpeg` and `fpcalc` (Chromaprint; `libchromaprint-tools` on Debian) on PATH, as the image has. The FLAC encode, WAV/AIFF and fingerprint tests use them and fail rather than skip without them.

## Running on Postgres
`--db=postgres` needs `MIXSYNC_TEST_DATABASE_URL`. It refuses to run unless the database name starts with `test_` or ends with `_test`, and refuses URLs whose query sets `dbname`, `service` or `options`. Each test gets its own schema, so `-n auto` is safe.
```
docker run -d --rm --name mixsync-pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=mixsync_test -p 55432:5432 postgres:16
export MIXSYNC_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55432/mixsync_test
uv run pytest -n auto --db=postgres
docker stop mixsync-pg-test
```
CI runs this in the `postgres` job, which also runs `-m crash --db=postgres`. SQLite-only tests (pragmas, single-writer lock) skip themselves. `-m integration` runs on SQLite only.

## Rules
- Unit tests never touch the network: a conftest fixture blocks sockets, so use fakes or [cassettes](cassettes/README.md).
- Service tests use fakes of the `core` Protocols, not mocks of adapter internals.
- Time is injected (`Clock` from `core`); retention, watchlist and backoff tests use a fake clock.
- Filesystem tests use `tmp_path` with a `/data`-shaped tree, never the real library.
