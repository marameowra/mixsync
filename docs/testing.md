# Testing

Required checks: [AGENTS.md](../AGENTS.md); source of truth `.github/workflows/ci.yml`. Per-folder detail is in the READMEs under `tests/`.

## CI (every push and PR)
- **Unit:** pytest, pytest-asyncio (auto), xdist. External APIs replay recorded JSON from `tests/cassettes/<service>/` ([README](../tests/cassettes/README.md)); tests never touch live services or Soulseek. Fakes for core Protocols, not mocks of adapter internals. Hypothesis covers path templating.
- **`postgres` job:** the suite with `--db=postgres`, then `pytest -m crash --db=postgres`.
- **Web:** FastAPI `TestClient`; the login form keeps `autocomplete="username"` / `"current-password"` and works with a plain submit.
- Soulseek fixtures should cover the [edge-case catalog](../tests/fixtures/README.md#soulseek-edge-case-catalog), tested by ID; a new real-world failure becomes a new case.
- Docs-only changes skip the code jobs.

## Crash-safety tests
`-m crash` (off by default, see `addopts`): SIGKILL the worker at each import step (after copy, fsync, tag write, before rename). Assert no partial file in `/data/library`, source intact, journal consistent, job resumes and completes. Same for trash moves and canonical-release merges.

Not built: regression baseline, compose integration tests, UI smoke tests (see issues).

## Matcher regression suite
Not built.

## Integration tests (compose)
Not built.

## UI tests
Not built (the `TestClient` tests above are what exist).
