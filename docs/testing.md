# Testing and validation

Matching quality and file safety get dedicated suites, because they are the two things MixSync must not get wrong. The CI steps agents must run are listed in [AGENTS.md](../AGENTS.md); `.github/workflows/ci.yml` is the source. Per-folder detail lives in the READMEs under `tests/`.

## What CI runs beyond the AGENTS.md list
- **`postgres` job:** the same suite with `--db=postgres`, then `pytest -m crash --db=postgres`.
- **`crash` marker:** SIGKILL tests, excluded from the default run (`addopts` in `pyproject.toml`). See below.
- **`integration` marker:** needs docker compose; excluded by default (no such tests yet).
- Docs-only changes skip both jobs.

## Unit tests
- pytest, pytest-asyncio (auto mode), pytest-xdist in CI.
- External APIs are replayed from recorded JSON responses in `tests/cassettes/<service>/*.json` ([README](../tests/cassettes/README.md)). Tests never touch live services.
- Hypothesis property tests cover path templating: no collisions, no illegal characters, stable output.
- Tests use fakes for the core Protocols, not mocks of adapter internals.

## Matcher regression suite
> Planned — not built (no issue yet): a labeled fixture set (CC-licensed clips with known MBIDs, recorded slskd results) scored per profile, with CI failing when auto-accept precision drops below a stored baseline. Raising the baseline would be a deliberate commit. Production overrides become new labels ([calibration](design/matching.md#calibration)).

## Soulseek in tests
CI never touches the Soulseek network. Unit tests feed the slskd adapter recorded JSON; the planned integration tests use a fake slskd that speaks the REST API. Fixtures should cover the [edge-case catalog](../tests/fixtures/README.md#soulseek-edge-case-catalog), tested by ID; a new real-world failure becomes a new case.

## Crash-safety tests (`-m crash`)
SIGKILL the worker at each import step (after copy, after fsync, after tag write, before rename). Assert: no partial file in `/data/library`, source intact, journal consistent, the job resumes and completes. Same pattern for trash moves and canonical-release merges.

## Integration tests (compose)
> Planned — not built (no issue yet): a compose file running slskd (or the fake), Navidrome and MixSync with throwaway volumes ([README](../tests/integration/README.md)). Flow: request an album, see it in Navidrome after rescan, push a discovery playlist, force expiry into `.trash`. On demand, not every push.

## UI tests
Web routes are tested with FastAPI's `TestClient`. The login form must keep `autocomplete="username"` / `"current-password"` and work with a plain form submit.

> Planned — not built: browser-driven smoke tests at phone (375 px) and desktop (1280 px) widths, including no horizontal scroll.
