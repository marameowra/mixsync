# `submit`

## Purpose
Close the loop for tracks MusicBrainz doesn't have: help the user add them to MusicBrainz, then promote them to verified once it has them.

## Owns / does not own
- **Owns:** Harmony link building (`harmony.py`) and MusicBrainz release-editor seed generation (`mb_seed.py`; `web` renders it as a form the user clicks).
- **Does not own:** submitting MusicBrainz edits (the user always submits with their own account), or the re-check and promotion. Those live in `acquire/recheck.py`, because `submit` may not import `acquire`; AcoustID submission is `acquire/acoustid_submit.py` ([ADR 0013](../../../docs/decisions/0013-one-acoustid-submission-key.md)).

## Rules / invariants
- The seed includes the edit note "Seeded by MixSync from local files." Field names were checked against MusicBrainz's Release Editor Seeding docs (URL in `mb_seed.py`).
- Re-checks are user-triggered first; the background schedule backs off ([design](../../../docs/design/unverified-and-mb-submit.md#re-check-and-promote)).
- Promotion is never automatic: a confident match only makes a draft plan the user confirms and can undo.

## May import
`core`, `db`, `match`, `library`.

## Design doc
[Unverified imports and MB submission](../../../docs/design/unverified-and-mb-submit.md)
