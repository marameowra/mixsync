# `match`

## Purpose
Decide how well a file or candidate matches a MusicBrainz target, in a way a human can read. This is the core of why MixSync exists.

## Owns / does not own
- **Owns:** features (`features.py`), the distance formula (`scorer.py`), bands and profiles (`profiles.py`), vetoes (`vetoes.py`), stage-1 candidate scoring (`candidates.py`), scorer versioning.
- **Does not own:** fetching anything. Callers pass in the evidence. **No I/O here.**

## Rules / invariants
- **Explainable:** every penalty carries a reason string that the review UI shows verbatim.
- **Versioned:** bump `SCORER_VERSION` on any change to weights or features; decisions record it.
- **No ML at runtime** ([ADR 0005](../../../docs/decisions/0005-explainable-matching-score.md)).
- Beets port: `features.py` and `scorer.py` are ported from beets' `autotag` (MIT), not imported ([ADR 0006](../../../docs/decisions/0006-port-beets-autotag.md)); attribution and the beets commit are in each file's header and must stay there. `candidates.py` is new code that reuses `Distance`, `assign_tracks` and `string_dist`.
- String similarity uses `rapidfuzz` on normalized strings (casefold, NFKC, strip "feat.", punctuation, bracketed suffixes).
- Multi-artist credits (`A & B`, `A feat. B`, ...) that include the expected artist get a flat 0.2 penalty (`artist_dist`), not a miss. On tags alone such a credit is capped at review, so only AcoustID-confirmed files auto-accept.
- AcoustID leads. A confident best result naming a track's recording keeps 10% of that track's title, artist, number and disc penalties and adds a zero `track_id` penalty; one naming a different recording adds a full `track_id` penalty and a veto. `best_release` lets confirmed recordings rank only releases within the profile's auto-accept threshold; tags decide between releases holding the same recordings.
- Vetoes (force review): duration off by more than 15 s; AcoustID naming a different recording. Stage 1 also vetoes missing tracks, no filename titles, and a FLAC/bitrate size mismatch; a single-file rip with a `.cue` is rejected outright.
- Partial releases: stage 1 caps a folder with missing tracks at review so a complete folder ranks first. Stage 2 charges missing tracks normally, except when AcoustID confirms every present file and the album tag is within 0.2 string distance of the release title: then they cost nothing and the files import without review (evidence `partial`). The request stays open as `partial`; `owned_groups` does not count the group as owned until it completes.
- Calibration (fitting weights from `match_decisions`) is described in the [design doc](../../../docs/design/matching.md#calibration); no calibration code exists yet.

## May import
`core` only (plus rapidfuzz).

## Design doc
[Matching](../../../docs/design/matching.md) · [ADR 0005](../../../docs/decisions/0005-explainable-matching-score.md) · [Regression suite](../../../docs/testing.md#matcher-regression-suite)
