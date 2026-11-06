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

Planned (#300): compose integration tests, UI smoke tests.

## Matcher regression suite
`tests/regression/` replays real logged decisions through the matcher offline and fails when any
case's band or chosen release moves from `tests/regression/baseline.json` (ADRs 0005, 0006). It
runs with the normal suite; on its own: `uv run pytest tests/regression`.

- **A case** (`cases/d<decision id>.json`) holds the matcher's inputs for one decision: the peer's
  folder for stage 1 (`score_candidate`), the downloaded files with tags, AcoustID results,
  high-band share and trailing silence for stage 2 (`score_files`, or `best_release` when a
  repick adds a second release), the user's profile, and the MusicBrainz release(s). `labels`
  says why it was picked (transcode, padded, AcoustID contradiction, repick, reject...);
  `recorded` is what production decided at the time, kept for calibration (#299), not checked.
- **A failure** prints one block per moved case: old and new band or release, distance, the
  largest penalties and the vetoes.
- **A deliberate change:** `uv run python -m devtools.regression.replay --update`, then commit the
  baseline diff with the scoring change so review sees every case that moved.
- **Adding cases:** run the read-only export inside the mixsync container, then build cases
  locally (fetches each release from MusicBrainz at 1 req/s; existing case files are kept):

  ```
  ssh elitedesk 'sudo podman exec -i mixsync nice python - 100' \
      < devtools/regression/export.py > inputs.jsonl
  uv run python -m devtools.regression.build_cases inputs.jsonl
  uv run python -m devtools.regression.replay --update
  ```

  The export hashes peer names, keeps only the last two path parts, and reads retagged files
  through their pre-import tag snapshot. Quotas per label are in `export.py`. Check new
  folder names for anything private before committing.
- **Limits:** only the matcher is replayed; pipeline rules (partial re-score, migration and
  unreadable-file overrides, release identification by search) are not. Decisions whose files
  were purged cannot be exported.

## Integration tests (compose)
Planned (#300).

## UI tests
Planned (#300); the `TestClient` tests above are what exist.
