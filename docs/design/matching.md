# Matching

The reasoning behind this design is in [ADR 0005](../decisions/0005-explainable-matching-score.md). Matching runs in two stages: pick a good candidate before downloading, then verify the file after.

## Stage 1: pre-download candidate scoring
Input: slskd search results (peer, folder, file list with names, sizes, bitrates, durations) and the target MB release/recording.

| Feature | What it measures |
|---|---|
| Folder completeness | Share of the MB tracklist present in the peer's folder; extra files count against it |
| Filename/title similarity | rapidfuzz ratio between parsed filename and MB track title/artist |
| Duration match | Per-track delta against MB length, when slskd reports duration |
| Quality fit | Format and bitrate against the user's quality preference |
| Size plausibility | File size consistent with the claimed format, bitrate, and duration |
| Peer health | Free upload slots, queue length, reported speed |

The best-scoring candidate is downloaded. No runners-up are stored. When a candidate is dropped (failed transfer, unusable files, rejection), it is added to the request's tried list and the retry runs a fresh search that skips every tried folder.

## Stage 2: post-download verification
| Feature | What it measures |
|---|---|
| AcoustID | Fingerprint (`fpcalc`) → AcoustID → recording MBIDs and score; does it include the expected recording? **Mandatory for every file:** existing tags never skip it, and an unreachable AcoustID means retry, not import. |
| Duration delta | Actual decoded length against MB length |
| Artist/title similarity | Existing tags and filename against MB |
| Track position | Track/disc number consistency |
| Album consistency | Do all files in the batch resolve to the same release/release group? |

## Scoring and bands
- Each feature yields a penalty in `[0, 1]` multiplied by a weight. The total is a **distance**, where 0 is a perfect match.
- Year tags matching either the release year or original release year have no penalty. When the original release year is known, other year differences are divided by the absolute span from that year to the current year, with a minimum span of one year, and clamped to `[0, 1]`. Without an original release year, a mismatch has penalty 1.
- The **requesting user's profile** sets two thresholds:

| Profile | Auto-accept if distance ≤ | Review if ≤ | Otherwise |
|---|---|---|---|
| Strict | 0.04 | 0.30 | reject |
| Balanced | 0.08 | 0.40 | reject |
| Loose | 0.15 | 0.55 | reject |

Users can edit their thresholds; the presets are starting points.

- **Hard vetoes** override the score. Examples: the AcoustID match is a *different* recording with a high score, or the duration is off by more than 15 seconds. These always go to review.
- **Reject** never deletes. The files go to quarantine and the next candidate is tried. Discovery requests treat a review-band match as a rejection.

## Quarantine
Quarantine is `/data/quarantine/<rejected|review>/<YYYY-MM-DD>/<folder>/<file>`, owned by MixSync ([ADR 0007](../decisions/0007-quarantine-owned-by-mixsync.md)). slskd's retention only touches `/downloads`, so nothing a person still has to review can vanish.
- When verify rejects a downloaded candidate (or finds an unusable file), its files move to `rejected/`. A review-band match moves to `review/`. The stored evidence and assignment paths point at the quarantine copies.
- Each file move is a journaled verified copy, then a journaled `release` of the `/downloads` source (the mounts differ). Quarantine uses the same path-safety rules as staging and trash.
- Accepting a review candidate imports from quarantine and then releases the quarantine copy like a released download. A person's reject leaves the files where they are; the decision's `final_action = reject` and `acted_at` mark them rejected.
- Migration, drop-folder and adopted files are not moved, and neither are rejected folders already in `/downloads`. The adopter scans only `/downloads`, so it never sees quarantine.
- For the purge ([#208](https://github.com/marameowra/mixsync/issues/208)): `rejected/<day>` folders are rejected entries, aged by the day in their name. `review/` entries whose decision a person rejected are rejected too, found through the decision's evidence paths and aged by `acted_at`. Other `review/` entries are never purged.

> [!question] Threshold values
> The numbers above are placeholders taken from beets' defaults (`strong_rec_thresh` 0.04, `medium_rec_thresh` 0.25). Tune them during phase 2 against the regression fixtures.

## Close runner-up
When several releases are scored, a match cannot auto-accept if another release, holding at least as many AcoustID-confirmed recordings as the winner, is within the profile's auto-accept limit of the winner's distance. It goes to review, and the decision's evidence names the runner-up and the gap.

## Review screen
Laid out side by side:
- **Source:** peer, folder, filename, format, bitrate, size, and for future sources the URL, uploader, and thumbnail
- **Candidate:** the MB release and recording, cover art, and track list
- **Evidence:** the per-feature penalty breakdown, the AcoustID result, and the duration delta
- **Preview:** inline `<audio>` player

**Actions:** Accept · Pick another release · Mark unverified ([details](unverified-and-mb-submit.md)) · Reject

## Calibration
- Every decision is stored with the feature vector, the scorer version, the band, and the final user action.
- Overrides (an auto-accept that was later undone, or a reviewed item that was accepted or rejected) are labeled data.
- After about 300 labels, `match/calibrate.py` fits a logistic regression on the same features. The proposed weights are evaluated on the regression suite and adopted only if precision improves at equal or better recall.

## beets reuse
The distance and candidate logic is **ported** from beets' `autotag` (MIT license) into `match/`, not embedded. See [ADR 0006](../decisions/0006-port-beets-autotag.md).
