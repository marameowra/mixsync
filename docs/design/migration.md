# Migration from an existing library

**Approach:** re-import. The old library is copied through the matcher into a new tree. The old tree is never modified and stays as a backup.

## Setup
- Mount the old library at `/import-source` read-only. The new library is `/data/library`; files are copies.
- No hardlinks: a tag write would change the original.

## Preflight (the import refuses to start if any check fails)
1. Walk `/import-source`: count files, total size, list unreadable files.
2. Free space on `/data/library` is at least 1.1x the source size.
3. Show an ETA (a lower bound: MB requests plus one AcoustID lookup per file, at the [service limits](services-etiquette.md)).
4. Require confirmation: the "other MusicBrainz clients paused" box ([why](services-etiquette.md#musicbrainz)) and, above 50 albums, the album count typed in.

## Flow
- One resumable job per album folder (or loose-file group), at lower priority than normal requests.
- Every file gets an AcoustID lookup ([ADR 0009](../decisions/0009-acoustid-mandatory.md)).
- Each folder runs [stage 2 matching](matching.md#stage-2-post-download-verification) with the admin's profile: auto-accept imports and canonicalizes; review and no-MB-match go to the review queue (the latter offering **Mark unverified**).
- An album with a `musicbrainz_albumid` is scored against that release (unknown id falls back to search); otherwise against the top 3 search hits. A reject band or no candidate goes to review, where reject skips the album.
- For large libraries consider a local MB mirror (`MIXSYNC_MB_BASE_URL`).
- WAV and AIFF are encoded to FLAC on import; Opus is tagged in place. Unsupported formats are listed as `skipped` (`unsupported format`). Extensions: `core/audio.py`.
- The source is only read, never moved, written or released. **Check old tree** re-hashes it against the preflight manifest.

## Report
Totals (imported, in review, unverified, failed with reasons), downloadable as CSV. Exit criterion: the old tree is byte-identical before and after.
