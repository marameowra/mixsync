# Testing and validation

Every change is validated automatically. Matching quality and file safety get their own dedicated suites, because they are the two things MixSync must not get wrong.

## CI (every push and PR)
| Check | Tool |
|---|---|
| Lint + format | ruff |
| Types | pyright (strict) |
| Unit tests | pytest, pytest-asyncio |
| External API tests | vcrpy cassettes for MusicBrainz, AcoustID, ListenBrainz, Last.fm, so CI never hits live services |
| Property tests | Hypothesis for path templating: no collisions, no illegal characters, stable output for the same input |
| Migrations | Alembic upgrade → downgrade → upgrade on an empty DB |

Pre-commit runs ruff and pyright locally.

## Matcher regression suite
- **Fixture set:** short CC-licensed audio clips with known MBIDs, plus recorded slskd search results (real-world filenames, folders, and bitrates).
- **Labels:** each fixture is marked as correct match, wrong match, or not on MB.
- **Metrics:** precision and recall per band (auto-accept / review / reject) for each matching profile.
- **Gate:** CI fails if auto-accept precision drops below the stored baseline. Raising the baseline is a deliberate commit.
- User overrides in production can be exported (anonymized) to grow the set. See [matching](design/matching.md#calibration).

## Integration tests (compose)
A compose file spins up slskd, Navidrome, and MixSync with test volumes.

1. Request a known album → it auto-imports with correct MBIDs.
2. It appears in Navidrome after the rescan.
3. A discovery playlist is pushed through the Subsonic API.
4. Forcing expiry moves the provisional file to `.trash`.

These run nightly and on demand, not on every push. They depend on the Soulseek network, so the default mode uses a local fixture share.

> [!question] Soulseek in CI
> Pick an option: point slskd at the real network with a test account, or stub slskd with a fake adapter that serves fixture search results. Recommendation: stub it in CI and use the real network only for manual runs.

## Crash-safety tests
- Kill the worker with SIGKILL at each step of an import (after copy, after fsync, after tag write, before rename).
- Assert there is no partial file in `/data/library`, the source is intact, the journal is consistent, and the job resumes and completes.
- Same pattern for trash moves and canonical-release merges.

## UI tests
- Playwright smoke test at phone (375 px) and desktop (1280 px) widths: login, request, review, playlist pages.
- The login form has `autocomplete="username"` and `autocomplete="current-password"`, and works with a real form submit.
- No horizontal scroll at the phone width.
