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

> [!question] Terms of service
> Discogs: per-user token, 60 req/min authenticated. Deezer: confirm the API terms allow metadata lookup for this use. Neither ships until confirmed.

## May import from
`core`, `ratelimit`.

## Tests
vcrpy cassettes per provider; the mapping to `TagSuggestion`.

## Design docs
[Unverified imports and MB submission](../../../../docs/design/unverified-and-mb-submit.md)
