# Matching

Rationale: [ADR 0005](../decisions/0005-explainable-matching-score.md), [0006](../decisions/0006-port-beets-autotag.md) (distance logic ported from beets' `autotag` into `match/`). Two stages: pick a candidate before downloading, verify the file after.

## Stage 1: pre-download candidate scoring
Input: slskd search results and the target MB release/recording. Features: folder completeness (extra files count against), filename/title similarity (rapidfuzz), per-track duration delta, quality fit against the user's preference, size plausibility, peer health (slots, queue, speed).

The best-scoring candidate is downloaded; the others are not stored. A dropped candidate (failed transfer, unusable files, rejection) joins the request's tried list, and the retry runs a fresh search that skips every tried folder.

## Stage 2: post-download verification
Features: AcoustID recording match (mandatory for every file, [ADR 0009](../decisions/0009-acoustid-mandatory.md)), decoded duration delta, artist/title similarity of tags and filename, track position, and whether all files resolve to the same release/release group.

## Scoring and bands
- Each feature gives a penalty in `[0, 1]` times a weight; the total is a distance, 0 being perfect.
- Year: matching the release year or original year is free. Otherwise, with a known original year, the difference is divided by the span from that year to now (minimum one year) and clamped to `[0, 1]`; with no original year a mismatch costs 1.
- The requesting user's profile sets two inclusive thresholds (values in `match/profiles.py`; users can edit them):

| Profile | Auto-accept if ≤ | Review if ≤ | Else |
|---|---|---|---|
| Strict | 0.04 | 0.30 | reject |
| Balanced | 0.08 | 0.40 | reject |
| Loose | 0.15 | 0.55 | reject |

- **Hard vetoes** force review whatever the distance: a high-scoring AcoustID match to a *different* recording, duration off by more than 15 s, a lossless file with almost nothing above 16 kHz (transcode), a file silent for over half its length at the end (padded preview).
- **Reject** never deletes: files go to quarantine ([data safety](data-safety.md#1-no-hard-deletes-in-the-normal-flow)) and the next candidate is tried. Discovery requests treat a review-band match as a rejection.

## Close runner-up
When several releases are scored, a match cannot auto-accept if another release with at least as many AcoustID-confirmed recordings is within the profile's auto-accept limit of the winner's distance. It goes to review and the decision's evidence names the close runner-up and the gap.

## Review screen
Side by side: source (peer, folder, file, format, bitrate, size), candidate (MB release and recording, cover, track list), evidence (per-feature penalties, AcoustID result, duration delta), and an inline audio preview. Actions: Accept, Pick another release, Mark unverified ([details](unverified-and-mb-submit.md)), Reject.

## beets reuse
The distance and candidate logic is ported, not embedded ([ADR 0006](../decisions/0006-port-beets-autotag.md)).

## Calibration
Every decision stores its feature vector, scorer version, band and final user action; overrides are labeled data. Weights are hand-tuned today; a fitting job is not built.
