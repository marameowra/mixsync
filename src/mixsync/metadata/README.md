# `metadata`

## Purpose
Adapters for metadata and fingerprint services: **MusicBrainz** (WS/2 JSON), **AcoustID**, and the **Cover Art Archive**. Read-only fallbacks live in [`fallback/`](fallback/README.md).

## Owns / does not own
- **Owns:** typed clients, request building (includes, paging), response → `core` type mapping, local fingerprinting (`fpcalc`).
- **Does not own:** deciding whether something matches (`match`), or storing results (`db`). Caching is handled by `ratelimit`.

## Proposed files
| File | Contents |
|---|---|
| `musicbrainz.py` | `MusicBrainzProvider`: search, lookup by MBID (`release`, `release-group`, `recording`, `artist`, `label`), browse (release groups by artist/label), paging |
| `acoustid.py` | `fingerprint(path)` (runs `fpcalc -json` in a thread), `lookup(fp, duration)`, `submit(fp, duration, recording_mbid)` |
| `coverart.py` | Front-cover URL and download for a release, falling back to the release group |
| `models.py` | pydantic models for the raw API JSON, mapped to `core` types at the edge |

## Key interface (`core.protocols.MetadataProvider`)
```
search_release_groups(artist, title) -> list[ReleaseGroupRef]
get_release(mbid, includes=recordings,media,labels) -> Release
get_release_group(mbid) -> ReleaseGroup (with releases)
browse_release_groups(artist_or_label_mbid, since) -> list[ReleaseGroupRef]
lookup_fingerprint(fp, duration) -> list[AcoustIdResult]   # acoustid
```

## Rules
- Every call goes through `ratelimit.polite_client(...)`; MusicBrainz is at 1 req/s.
- Genres come from MusicBrainz's curated genres (`inc=genres`) on release groups, releases, and artists. Folksonomy `tags` are never used for paths ([library design](../../../docs/design/library.md#path-templates)).
- Request only the `inc=` parameters actually needed. Big includes are slow for MusicBrainz too.
- Browse endpoints page at `limit=100` and stop at the date filter, rather than fetching everything.
- AcoustID submissions use the **user's** key from `user_links`, never the app key.
- Raw API JSON is validated with pydantic at the edge. Unknown fields are ignored, never trusted.
- An optional `Settings.mb_base_url` points at a local MusicBrainz mirror.

## May import from
`core`, `ratelimit`.

## Tests
- `tests/unit/metadata/`: vcrpy cassettes for each endpoint; mapping tests; paging stop conditions.
- `fpcalc` wrapper against `tests/fixtures/audio/` clips.

## Design docs
[Matching](../../../docs/design/matching.md) · [Respecting services](../../../docs/design/services-etiquette.md)
