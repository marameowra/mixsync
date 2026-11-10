# Matching

Rationale: [ADR 0005](../decisions/0005-explainable-matching-score.md), [0006](../decisions/0006-port-beets-autotag.md) (distance logic ported from beets' `autotag` into `match/`). Two stages: pick a candidate before downloading, verify the file after.

## Stage 1: pre-download candidate scoring
Input: slskd search results and the target MB release/recording. Features: folder completeness (extra files count against), filename/title similarity (rapidfuzz), per-track duration delta, quality fit against the user's preference, size plausibility, peer health (slots, queue, speed).

### Quality fit
- With no preference the order is lossless first, then higher bitrate, with fixed penalties (lossless 0, lossy 256+ kbps 0.2, 192+ 0.4, below 1).
- A user's preference (`matching_profiles.quality_pref`, set on `/account`) is an ordered list of tiers, best first, and an optional floor in kbps for lossy files. Tiers (`QUALITY_TIERS` in `match/candidates.py`): `lossless`, `mp3_320`, `mp3_v0` (VBR MP3, 220 kbps or more), `mp3_256`, `lossy_192` (any lossy, 192 kbps or more). A tier left off the list is not accepted. A blank or invalid preference means the built-in order.
- Each file takes the penalty `place / tiers` for the first listed tier it satisfies, and the full penalty when none fits (an unknown bitrate ranks just behind the listed tiers). The breakdown's `quality_fit` reasons name the choice, e.g. "mp3 320 kbps is quality choice 2 of 2". Per-track dedupe keeps the better-placed copy.
- Skips: a lossy file under the floor skips the whole folder (lossless always clears it; an unknown bitrate is not counted as under it), and a folder is skipped when more than half its scored files are outside the list. A skipped folder is REJECT with a veto starting `quality preference:`, so it is never downloaded; when that leaves nothing, the request's `no_results` note says how many folders the preference skipped.

The best-scoring candidate is downloaded; the others are not stored. A dropped candidate (failed transfer, unusable files, rejection) joins the request's tried list, and the retry runs a fresh search that skips every tried folder. A search tries each enabled download source in the source priority (`/account/sources`) and stops at the first with an acceptable candidate; the no-results backoff starts only when all came up empty.

## Stage 2: post-download verification
Features: AcoustID recording match (mandatory for every file, [ADR 0009](../decisions/0009-acoustid-mandatory.md)), decoded duration delta, artist/title similarity of tags and filename, track position, and whether all files resolve to the same release/release group.

## Scoring and bands
- Each feature gives a penalty in `[0, 1]` times a weight; the total is a distance, 0 being perfect.
- Year: matching the release year or original year is free. Otherwise, with a known original year, the difference is divided by the span from that year to now (minimum one year) and clamped to `[0, 1]`; with no original year a mismatch costs 1.
- The requesting user's profile sets two inclusive thresholds (preset values in `match/profiles.py`):

| Profile | Auto-accept if ≤ | Review if ≤ | Else |
|---|---|---|---|
| Strict | 0.04 | 0.30 | reject |
| Balanced | 0.08 | 0.40 | reject |
| Loose | 0.15 | 0.55 | reject |

- The UI shows a limit as confidence, `round((1 - distance) × 100)`: 0.08 is 92%. On the account page (`/account`) a user sees both limits and can set their auto-accept confidence, from the review limit up to 100%. Saving stores a `custom` `matching_profiles` row only that user references (a shared row is copied first); `review_max` is carried over and not editable. A user without a row runs Balanced.
- The same section has a preset picker (Strict, Balanced, Loose) showing each preset's limits as confidence. Choosing one writes the user's own `matching_profiles` row (name and `preset` = the preset, both limits = its values; no shared rows are seeded), replacing any custom limits. Editing the auto-accept limit afterwards makes the row `custom`, and the picker shows Custom. No row shows Balanced. A preset looser than the admin minimum is flagged on the page; the minimum still holds through the profile loader. A new choice affects new scoring and Recheck only; existing decisions are not re-banded.
- An admin sets an app-wide minimum auto-accept confidence at `/settings` (the `app_settings` table; empty means no minimum, the default). Saving it lowers every stored auto-accept limit looser than it, and the profile loader holds a user without a row to it too. Clearing it leaves lowered rows as they are.
- A new limit or minimum applies to new scoring and Recheck only; decisions already in review are not re-banded.

- **Hard vetoes** force review whatever the distance: a high-scoring AcoustID match to a *different* recording, duration off by more than 15 s, a lossless file with almost nothing above 16 kHz (transcode), a file silent for over half its length at the end (padded preview).
- **Reject** never deletes: files go to quarantine ([data safety](data-safety.md#1-no-hard-deletes-in-the-normal-flow)) and the next candidate is tried. Discovery requests treat a review-band match as a rejection.

## Close runner-up
When several releases are scored, a match cannot auto-accept if another release with at least as many AcoustID-confirmed recordings is within a fixed gap of 0.08 (`RUNNER_UP_GAP`) of the winner's distance. It goes to review and the decision's evidence names the close runner-up and the gap. The same 0.08 bounds which releases compete on AcoustID-confirmed counts. The gap is the same for every profile, so a user's auto-accept limit never widens or narrows it.

## Review screen
Side by side: source (peer, folder, file, format, bitrate, size), candidate (MB release and recording, cover, track list), evidence (per-feature penalties, AcoustID result, duration delta), and an inline audio preview. Actions: Accept, Pick another release, Split off files, Mark unverified ([details](unverified-and-mb-submit.md)), Reject. Split takes files the release has no track for and makes them a new from-files request (`source="split"`) verified against one release the user picks, offered from the releases AcoustID placed them on or pasted; the decision stays pending for the rest and is not re-scored, and every later action on it (refile, repick, re-check, unverified) leaves the split-off files alone. Split is refused for discovery and library-migration reviews, for every file left (pick another release instead) and for the review's own release; a split whose release MusicBrainz does not have becomes a review of its own, so its files stay rejectable. No unsplit: reject the new request's review instead.

## beets reuse
The distance and candidate logic is ported, not embedded ([ADR 0006](../decisions/0006-port-beets-autotag.md)).

## Calibration
Every decision stores its feature vector, scorer version, band and final user action; overrides are labeled data. Weights are hand-tuned today.

Run `uv run python -m devtools.calibrate <copy of the DB, SQLite path or URL>` once about 300 accept/reject overrides have piled up (below `--min-samples`, default 300, it warns and exits non-zero unless `--force`). It opens the database read-only, fits non-negative weights by logistic regression on each decision's per-feature distance shares (the accept/reject boundary stays at the balanced review limit), and prints current and proposed weights, the sample count and agreement with human outcomes before and after. The agreement is in-sample and approximate: the logged breakdown omits zero-penalty features, so each decision keeps its logged max distance, while the real scorer renormalises by the new weights. The regression suite is the real check. It refuses to fit when every decision has the same outcome. It changes nothing: to adopt the result, edit `DEFAULT_WEIGHTS` in a normal PR, which the regression suite ([testing](../testing.md)) must pass, updating its baseline only for deliberate changes. Features the logged decisions never exercise keep their weights.
