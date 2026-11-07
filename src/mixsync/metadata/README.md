# `metadata`

## Purpose
Adapters for MusicBrainz (WS/2 JSON), AcoustID and the Cover Art Archive, plus local fingerprinting (`fpcalc`).

## Owns / does not own
- **Owns:** typed clients, request building (includes, paging), mapping raw JSON to `core` types (`models.py`), `fingerprint()`.
- **Does not own:** deciding whether something matches (`match`), storing results (`db`), or caching (`ratelimit`).

## Rules / invariants
- Every call goes through `ratelimit.PoliteClient`. Limits and User-Agent: [services etiquette](../../../docs/design/services-etiquette.md).
- Request only the `inc=` parameters needed, and always `fmt=json`.
- **A lookup returns at most 25 linked entities per `inc=`.** Lists (an artist's release groups, a label's releases) come from browse or search, never from a lookup include.
- **Browse results are ordered by MBID, not date**, so you cannot stop early at a date. Browse all pages at `limit=100`, advancing `offset` by the number of items received.
- "What's new since X" is a search with a date range, one request per follow. The index can lag, so the watchlist overlaps its window by 7 days.
- Genres come from MusicBrainz's curated genres (`inc=genres`), never folksonomy tags ([ADR 0014](../../../docs/decisions/0014-default-path-template-and-genres.md)).
- No MusicBrainz API writes. Edits go through release-editor seeding in the browser ([submit](../submit/README.md)).
- Raw JSON is validated with pydantic at the edge; unknown fields are ignored, never trusted.
- AcoustID lookups need `Settings.acoustid_app_key`; submissions use the deployment key ([ADR 0013](../../../docs/decisions/0013-one-acoustid-submission-key.md)). `Settings.mb_base_url` can point at a local MusicBrainz mirror.
- Tests replay recorded JSON from `tests/cassettes/<service>/` through `httpx.MockTransport` ([cassettes](../../../tests/cassettes/README.md)).

`deezer.py` is a read-only client (album search, album tracklist) behind `Settings.deezer_suggestions`; it only suggests tags for Mark unverified. `discogs.py` is the same for Discogs (release search, release tracklist) behind `Settings.discogs_suggestions`, using the reviewer's own token. Terms and limits are in [services etiquette](../../../docs/design/services-etiquette.md#discogs-and-deezer-fallbacks-off-by-default).

## May import
`core`, `ratelimit`.

## Design doc
[Matching](../../../docs/design/matching.md) · [Respecting services](../../../docs/design/services-etiquette.md)
