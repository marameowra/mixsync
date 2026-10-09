# `tests/cassettes`

vcrpy recordings of real HTTP exchanges with MusicBrainz, AcoustID, Cover Art Archive, and ListenBrainz. Unit tests replay them, so CI never hits live services.

## Layout
`tests/cassettes/<service>/<test_name>.yaml`

## Recording
- Default record mode in CI is `none` (fail on any unrecorded request).
- To record or refresh locally: `uv run pytest --record-mode=once tests/unit/metadata/test_x.py`.
- Recording respects `ratelimit`, so a refresh of many cassettes is slow on purpose.

## Rules
- **Scrub secrets** before commit: API keys, tokens, usernames, and cookies are filtered by `vcr_config` (`filter_headers`, `filter_query_parameters`, `before_record_response`). A CI check greps cassettes for known key patterns.
- Re-record when an API's response shape changes or a test's inputs change. Not routinely.
- Keep cassettes small: request only the `inc=` fields the test needs.

## Design docs
[Testing: CI](../../docs/testing.md#ci-every-push-and-pr)
