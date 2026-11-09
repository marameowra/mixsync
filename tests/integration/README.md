# `tests/integration`

End-to-end tests against real containers: MixSync (web and worker, built from `deploy/Dockerfile`),
Navidrome (the image pinned in `deploy/compose.yml`) and a fake slskd.

## Run

```
tests/integration/run.sh            # extra arguments go to pytest, e.g. -k happy
```

Needs Docker with Compose v2. It builds the images, starts the stack in `compose.yml`, runs pytest
in a runner container on the stack's network, prints the service logs on failure, and then removes
the stack and its volumes (throwaway test data only). A run takes a few minutes, most of it the
first build.

## What runs
- `fakes/server.py`: one fake for slskd (search, responses, enqueue, transfer status, cancel),
  MusicBrainz (`/ws/2/release`), AcoustID (`/v2/lookup`, by the file's length) and the Cover Art
  Archive (always 404). It serves two canned albums and generates their short noise FLACs at
  startup; an enqueue copies them into the downloads volume where slskd would save them.
- `test_stack.py`: the happy path (the album lands in `/data/library` and Navidrome's
  `getAlbumList2` lists it after mixsync's rescan), a review-band path (low-passed files trip the
  transcode veto: the album lands in review, not the library), and UI smoke checks (login, a
  request, the review list and detail pages).

## Rules
- Marked `@pytest.mark.integration`; the default run and `scripts/check.sh` deselect them
  (`addopts` in `pyproject.toml`). CI runs them in the `integration` workflow on PRs touching
  `src/mixsync/sources/`, `src/mixsync/acquire/`, `deploy/` or this folder, and on demand. It is
  not a required check.
- No live services. Every container sits on an `internal` Docker network, so nothing can reach
  Soulseek, MusicBrainz, AcoustID or the Cover Art Archive. MusicBrainz and slskd are pointed at
  the fake with their normal settings; AcoustID and the Cover Art Archive need
  `MIXSYNC_TEST_ONLY_FAKE_SERVICES_URL`, which is test-only: never set it in a deployment (startup
  logs a warning when it is set).
- The fake follows slskd's real response shapes ([ADR 0016](../../docs/decisions/0016-fake-slskd-in-ci.md));
  `SlskdSource` is unchanged for it. A new real-world failure becomes a new
  [edge-case catalog](../fixtures/README.md#soulseek-edge-case-catalog) ID and a fake behaviour.

> Not built yet: the catalog's failure cases in the fake (slow peers, failed transfers,
> disconnects, empty results), discovery and crash-restart scenarios.

## Design doc
[Testing: integration](../../docs/testing.md#integration-tests-compose)
