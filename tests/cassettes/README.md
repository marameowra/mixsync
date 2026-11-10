# `tests/cassettes`

## Purpose
Recorded JSON bodies from MusicBrainz, AcoustID, Deezer, Discogs, ListenBrainz, Prowlarr, qBittorrent and SABnzbd. Unit tests read them and serve them through an `httpx.MockTransport` handler, so CI never hits live services. There is no replay library: a cassette is a plain `.json` file.

## Layout
`tests/cassettes/<service>/<name>.json`, one response body per file.

## Rules
- Recording is manual: fetch the response by hand (respecting [rate limits](../../docs/design/services-etiquette.md)) and save the body.
- Scrub API keys, tokens, usernames and cookies before commit. Some files are hand-written to the documented shape instead of recorded (the AcoustID fixture is; the Prowlarr ones follow its `ReleaseResource`, verified against Prowlarr source, develop @ 61476876ec3cb4526c21782f468a205ce608e7a9 (ReleaseResource.cs, SearchController.cs:198-199, NewznabController.cs:265-314, STJson WhenWritingNull), with a small `.torrent` and `.nzb`; the Usenet result omits its null fields as Prowlarr does; the qBittorrent one follows its official WebUI API docs; the SABnzbd ones (`queue`, `history`, `addfile`) follow its official API docs, each list with a user's same-named job in another category).
- Re-record when an API's response shape changes or a test's inputs change, not routinely.
- Keep them small: request only the `inc=` fields the test needs.

## Design doc
[Testing](../../docs/testing.md)
