# `tests/fixtures`

Test data shared by the unit and integration tests.

## Layout
```
audio/                      short CC-licensed clips + manifest (see audio/README.md)
slskd/                      recorded slskd search responses (JSON), added with the slskd adapter
labeled/                    matcher regression cases: candidate/file + MB target + expected band
regression_baseline.json    stored precision/recall per band per profile
```
Only `audio/` exists now. Create the others when the first test needs them.

## Soulseek edge-case catalog
Fake Soulseek data (`slskd/`) must cover every case below. Each case has a stable ID used in test names (e.g. `test_scoring[SK-F03]`) and an expected outcome: the chosen candidate, the band, or a specific error. Real-world failures get added as new IDs; IDs are never reused.

### Names and text
| ID | Case | Expected |
|---|---|---|
| SK-N01 | Diacritics / NFC vs NFD (`Björk` vs `Björk`) | matches |
| SK-N02 | Non-Latin scripts (CJK, Cyrillic) in folder and file names | matches against MB's original-script title |
| SK-N03 | `feat.` / `ft.` / `featuring` in title or artist | normalized, matches |
| SK-N04 | Bracketed suffixes: `(Remastered 2011)`, `[Deluxe]`, `(Explicit)` | matches the base title; no false version match |
| SK-N05 | Same title, different artist (common song names) | rejected, or the right artist ranked first |
| SK-N06 | Live / remix / acoustic / instrumental / karaoke / cover versions of the requested track | never auto-accepted as the studio version |
| SK-N07 | Filenames with no metadata (`01.mp3`, `track 1.flac`) | scored on position + duration only; review band at best |
| SK-N08 | Illegal or odd characters (`: ? * " / \`, trailing dots, very long names) | sanitized path, no crash |

### Folder structure
| ID | Case | Expected |
|---|---|---|
| SK-F01 | Complete album, clean names | auto-accept |
| SK-F02 | Missing tracks (e.g. 9 of 12) | lower completeness, ranked below complete folders |
| SK-F03 | Extra files: `.cue`, `.log`, `.nfo`, cover images, `.m3u` | ignored; non-audio files are not counted as tracks |
| SK-F04 | Multi-disc in subfolders (`CD1/`, `Disc 2/`) | grouped as one candidate; disc numbers mapped |
| SK-F05 | Multi-disc flattened into one folder with `1-01`, `2-01` numbering | disc numbers parsed |
| SK-F06 | Mixed formats in one folder (FLAC + MP3) | duplicate tracks resolved by quality preference |
| SK-F07 | A different release of the same album (deluxe with bonus tracks, regional edition) | matched to the release group; canonicalization applies |
| SK-F08 | A compilation / Various Artists folder | album artist = Various Artists; per-track artists matched |
| SK-F09 | A single-file album rip (one FLAC + `.cue`) | unsupported in v1 → reject with a clear reason |
| SK-F10 | A loose single track, not in an album folder | track-level request matching |

### Audio quality and file integrity
| ID | Case | Expected |
|---|---|---|
| SK-Q01 | Claimed FLAC whose size implies a lossy transcode | size-plausibility penalty; review |
| SK-Q02 | Reported bitrate that doesn't match the file | penalty; never auto-accept |
| SK-Q03 | Duration off by a few seconds (pregap, silence) | small penalty, still matches |
| SK-Q04 | Duration off by > 15 s (wrong edit or hidden track) | veto → review |
| SK-Q05 | Truncated or corrupt file (decode fails, fingerprint fails) | reject; next candidate tried |
| SK-Q06 | Zero-byte file | reject |
| SK-Q07 | Wrong extension (MP3 data named `.flac`) | detected by content; penalty |
| SK-Q08 | Existing tags that are wrong (tagged as another album) | fingerprint wins over tags |

### Peers and transfers (fake slskd behavior)
| ID | Case | Expected |
|---|---|---|
| SK-P01 | No search results | backoff schedule starts (1 h → 6 h → 24 h → weekly) |
| SK-P02 | Best candidate's peer has no free slots or a long queue | peer-health penalty; a healthy peer is preferred |
| SK-P03 | Transfer fails or is rejected by the peer | the next candidate is tried; the failure is recorded |
| SK-P04 | Peer disconnects mid-transfer (partial file) | partial file never imported; retry or next candidate |
| SK-P05 | Very slow transfer | progress shown; no timeout while bytes keep arriving |
| SK-P06 | slskd API errors (500, timeout, auth failure) | `TransientError` retried; auth failure surfaced to admin |
| SK-P07 | Duplicate search requests within 10 min | debounced to one search |
| SK-P08 | Many folders from one peer | at most one album queued per peer |

## Rules
- No copyrighted audio. Ever. Clips must be CC0/CC-BY/CC-BY-SA, or generated test tones.
- Recorded slskd responses are scrubbed of real peer usernames (replace them with `peer-001`, …).
- Each labeled case has one line explaining why its expected band is correct.
- Total fixture size stays under 50 MB. Larger corpora live outside the repo and are fetched by a script.

## Design docs
[Testing: matcher regression suite](../../docs/testing.md#matcher-regression-suite)
