# `submit`

## Purpose
Close the loop for tracks MusicBrainz doesn't have. Help the user add them to MB, then promote them to verified once MB has them.

## Owns / does not own
- **Owns:** Harmony link building, MB release-editor seed generation, the weekly re-check job, AcoustID fingerprint submission after promotion.
- **Does not own:** submitting MB edits. The user always submits in MB with their own account.

## Proposed files
| File | Contents |
|---|---|
| `harmony.py` | `harmony_url(store_url) -> str` |
| `mb_seed.py` | `build_seed(tracks) -> SeedForm` (field name → value); rendered by `web` as an auto-submitting form POSTing to `https://musicbrainz.org/release/add` in a new tab |
| `recheck.py` | Job `submit.recheck(track_id)`: search MB + AcoustID; a confident match → a "Promote to verified" review item with a tag diff |
| `promote.py` | After the user confirms: retag through `library`, set `status=verified`, then submit the fingerprint to AcoustID with the user's key |

## Rules
- The seed includes an edit note: "Seeded by MixSync from local files."
- Re-checks are **user-triggered first** (Check MusicBrainz now). The background schedule backs off, 1 week → 2 weeks → 1 month → quarterly, randomized within each interval and grouped per album ([design](../../../docs/design/unverified-and-mb-submit.md#re-check-and-promote)).
- Promotion is never automatic; it always goes through the review queue.

> [!question] Seed field names
> Verify against the current MusicBrainz "Release Editor Seeding" documentation before implementing `mb_seed.py`.

## May import from
`core`, `db`, `match`, `library` (service rule); `MetadataProvider` injected.

## Tests
- `build_seed` snapshot tests (multi-disc, missing durations, unicode).
- Re-check with a fake provider: no match → nothing; confident match → a review item; promotion → verified + AcoustID submit called once.

## Design docs
[Unverified imports and MB submission](../../../docs/design/unverified-and-mb-submit.md)
