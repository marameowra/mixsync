# `metadata`

## Purpose
Adapters for metadata and fingerprint services: **MusicBrainz** (WS/2 JSON), **AcoustID**, and the **Cover Art Archive**. Read-only fallbacks live in [`fallback/`](fallback/README.md).

## Owns / does not own
- **Owns:** typed clients, request building (includes, paging), response → `core` type mapping, local fingerprinting (`fpcalc`).
- **Does not own:** deciding whether something matches (`match`), or storing results (`db`). Caching is handled by `ratelimit`.

## Proposed files
| File | Contents |
|---|---|
| `musicbrainz.py` | `MusicBrainzProvider`: `get_release`, `search_releases` (more lookups, browse and paging land with the watchlist) |
| `acoustid.py` | `fingerprint(path)` (async `fpcalc -json` subprocess), `AcoustIdClient.lookup(fp)`; `submit` is not built yet |
| `coverart.py` | Front-cover URL and download for a release, falling back to the release group |
| `models.py` | pydantic models for the raw API JSON, mapped to `core` types at the edge |

## Key interface (`core.protocols.MetadataProvider`)
```
search_release_groups(artist, title) -> list[ReleaseGroupRef]
get_release(mbid, includes=recordings,media,labels) -> Release
get_release_group(mbid) -> ReleaseGroup (with releases)
new_release_groups_by_artist(artist_mbid, since) -> list[ReleaseGroupRef]   # search: arid + firstreleasedate range
new_releases_by_label(label_mbid, since) -> list[ReleaseRef]                 # search: laid + date range
browse_release_groups(artist_mbid) -> list[ReleaseGroupRef]                  # full browse, all pages (rarely)
(AcoustID is separate: AcoustIdClient.lookup(fp) -> list[AcoustIdResult])
```

## Rules
- Every call goes through `ratelimit.polite_client(...)`; MusicBrainz is at 1 req/s.
- Genres come from MusicBrainz's curated genres (`inc=genres`) on release groups, releases, and artists. Folksonomy `tags` are never used for paths ([library design](../../../docs/design/library.md#path-templates)).
- Request only the `inc=` parameters actually needed. Big includes are slow for MusicBrainz too.
- Always send `fmt=json`.
- **Lookups return at most 25 linked entities per `inc=`.** Lists (an artist's release groups, a label's releases) come from browse or search, never from a lookup include.
- **Browse results are ordered by MBID, not date**, so you can't stop early at a date. A browse is all pages at `limit=100`, advancing `offset` by the number of items *received* (release browses cap at about 500 tracks per response).
- **"What's new since X" uses search with a date range**, which is one request per follow:
  - artist: `/ws/2/release-group?query=arid:<mbid> AND firstreleasedate:[<since> TO *]&fmt=json`
  - label: `/ws/2/release?query=laid:<mbid> AND date:[<since> TO *]&fmt=json`

  Verify the range syntax against the live API when building the MusicBrainz client. The search index can lag the database slightly; the watchlist overlaps its `since` window by 7 days to cover that.
- **No MusicBrainz API writes.** Edits go through release-editor seeding in the browser ([submit](../submit/README.md)).
- AcoustID submissions use the **user's** key from `user_links`, never the app key.
- Raw API JSON is validated with pydantic at the edge. Unknown fields are ignored, never trusted.
- `Settings.acoustid_app_key` (`MIXSYNC_ACOUSTID_APP_KEY`) is required for lookups. An optional `Settings.mb_base_url` points at a local MusicBrainz mirror.

## May import from
`core`, `ratelimit`.

## Tests
- `tests/unit/metadata/`: recorded JSON in `tests/cassettes/<service>/` served through `httpx.MockTransport` (no vcrpy; the AcoustID fixture is hand-written); mapping tests; paging stop conditions.
- `fpcalc` wrapper with a faked subprocess; one test runs the real binary and is skipped when it is missing.

## Design docs
[Matching](../../../docs/design/matching.md) · [Respecting services](../../../docs/design/services-etiquette.md)
