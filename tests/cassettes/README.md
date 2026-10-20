# `tests/cassettes`

## Purpose
Recorded JSON bodies from MusicBrainz, AcoustID and ListenBrainz. Unit tests read them and serve them through an `httpx.MockTransport` handler, so CI never hits live services. There is no replay library: a cassette is a plain `.json` file.

## Layout
`tests/cassettes/<service>/<name>.json`, one response body per file.

## Rules
- Recording is manual: fetch the response by hand (respecting [rate limits](../../docs/design/services-etiquette.md)) and save the body.
- Scrub API keys, tokens, usernames and cookies before commit. Some files are hand-written to the documented shape instead of recorded (the AcoustID fixture is).
- Re-record when an API's response shape changes or a test's inputs change, not routinely.
- Keep them small: request only the `inc=` fields the test needs.

## Design doc
[Testing](../../docs/testing.md)
