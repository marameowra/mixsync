# `metadata/fallback`

## Purpose
**Read-only** suggestion sources for tracks that MusicBrainz doesn't have. They pre-fill the "Mark unverified" form. They are never a source of truth and never auto-applied.

## Proposed files
| File | Contents |
|---|---|
| `discogs.py` | Release search + lookup (needs a user token) |
| `deezer.py` | Album/track search |
| `base.py` | `FallbackProvider` Protocol: `suggest(artist, title, duration) -> list[TagSuggestion]` |

## Rules
- **Off by default.** Each provider is enabled per install after its terms are confirmed.
- Results are shown as suggestions with the source labeled; the user picks or edits.
- Rate limits are registered in `ratelimit/services.py` before the provider ships.

**Terms (checked 2026-10-09; details in [services etiquette](../../../../docs/design/services-etiquette.md#discogs-fallback-off-by-default)):**
- **Discogs:** 60 req/min authenticated, per IP; a unique UA is required; per-user personal token. MixSync uses 50/min. Re-check the official page by hand before enabling, because it blocked automated fetching.
- **Deezer:** no published quota; content use is limited to "strictly private use within a family scope", non-commercial. MixSync allows it only for single-household installs, metadata suggestions only, never audio.
- Both stay **off by default**.

## May import from
`core`, `ratelimit`.

## Tests
vcrpy cassettes per provider; the mapping to `TagSuggestion`.

## Design docs
[Unverified imports and MB submission](../../../../docs/design/unverified-and-mb-submit.md)
