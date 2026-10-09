# `tests/integration`

End-to-end tests against real containers: slskd, Navidrome, and MixSync.

## Proposed files
| File | Contents |
|---|---|
| `compose.test.yml` | slskd, navidrome, mixsync-web, mixsync-worker with throwaway volumes under a temp `/data` |
| `conftest.py` | Brings compose up/down, waits for health, provides API clients |
| `test_pipeline.py` | Request a known album → auto-import with correct MBIDs → visible in Navidrome |
| `test_discovery.py` | Playlist pushed through Subsonic → forced expiry → file in `.trash` |
| `test_crash.py` | Kill the worker container mid-import → restart → nothing lost, job completes |

## Rules
- Marked `@pytest.mark.integration`; skipped unless `-m integration`.
- Runs nightly and on demand in CI, not on every push.
- Default mode: slskd is replaced by a **fake slskd** serving fixture search results and files, so tests are deterministic and don't use the Soulseek network.

> [!question] Soulseek in CI
> Stub in CI and use the real network only for manual runs (the proposal). See [testing](../../docs/testing.md#integration-tests-compose).

## Design docs
[Testing: integration](../../docs/testing.md#integration-tests-compose)
