# `match`

## Purpose
Decide how well a file or candidate matches a MusicBrainz target, in a way a human can read. This is the core of why MixSync exists.

## Owns / does not own
- **Owns:** feature computation, the distance formula, bands, hard vetoes, profiles, scorer versioning, calibration.
- **Does not own:** fetching anything. Callers pass in the evidence (MB release, fingerprint results, file info). **This module does no I/O.**

## Proposed files
| File | Contents |
|---|---|
| `features.py` | One pure function per feature, each returning a penalty in `[0, 1]` plus a human-readable reason |
| `scorer.py` | `score(evidence, weights) -> MatchResult` (distance, per-feature breakdown, vetoes); `SCORER_VERSION` |
| `profiles.py` | Strict / balanced / loose presets; `band(distance, vetoes, profile) -> Band` |
| `candidates.py` | Stage 1: `score_candidate`, `rank` (one folder per peer), filename parsing, `STAGE1_WEIGHTS` |
| `vetoes.py` | Hard rules that force review (different recording with a high AcoustID score; duration off by > 15 s) |
| `calibrate.py` | Offline: fit a logistic regression on `match_decisions`, report precision/recall, propose new weights |

## Features
| Stage | Feature | Input |
|---|---|---|
| 1 (pre-download) | folder_completeness, title_similarity, duration_match, quality_fit, size_plausibility, peer_health | `Candidate` + MB release |
| 2 (post-download) | acoustid, duration_delta, artist_title_similarity, track_position, album_consistency | `AudioFileInfo` + tags + AcoustID results + MB release |

## Key interface
```
score_candidate(candidate, album, profile) -> MatchResult          # stage 1
score_files(files, release, acoustid_results, profile) -> MatchResult # stage 2
MatchResult: distance, band, breakdown[(feature, penalty, weight, reason)], vetoes, scorer_version
```

## Rules
- **Explainable:** every penalty carries a reason string that the review UI shows verbatim.
- **Versioned:** bump `SCORER_VERSION` on any change to weights or features. Decisions record the version.
- **No ML at runtime** until calibration proves itself on the regression suite ([ADR 0005](../../../docs/decisions/0005-explainable-matching-score.md)).
- String similarity uses `rapidfuzz` on normalized strings (casefold, NFKC, strip "feat.", punctuation, bracketed suffixes).

## beets port
The distance and candidate logic in `features.py` / `scorer.py` is ported from beets' `autotag` (MIT license), not imported ([ADR 0006](../../../docs/decisions/0006-port-beets-autotag.md)). Keep beets' copyright and permission notice in each ported file's header, and note the beets commit the port is based on.

## beets credit
`features.py` and `scorer.py` are ported from beets (MIT, Copyright (c) 2010-2016 Adrian Sampson), commit `b8c9661fe10463cc761b83f8efd861cb49224224`. Departures: no unidecode (NFKD + `isalnum`, so non-Latin text still compares), a pure-Python Hungarian assignment instead of lap/numpy, no data_source or preferred country/media options.

The AcoustID veto arrives in a later slice. Stage 1 (`candidates.py`) is new code, not a beets port; it reuses `Distance`, `assign_tracks`, and `string_dist`. Its vetoes (missing tracks, no filename titles, FLAC/bitrate size mismatch) force review, and a single-file rip with a `.cue` is rejected outright.

## May import from
`core` only (plus rapidfuzz).

## Tests
- `tests/unit/match/`: each feature against hand-built cases; banding at threshold edges; vetoes.
- **Regression suite:** `tests/fixtures/` labeled set → precision/recall per band per profile; CI fails below baseline ([testing](../../../docs/testing.md#matcher-regression-suite)).

## Design docs
[Matching](../../../docs/design/matching.md) · [ADR 0005](../../../docs/decisions/0005-explainable-matching-score.md)
