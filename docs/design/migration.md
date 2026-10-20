# Migration from an existing library

**Approach:** re-import. The old library is copied through the matcher into a new tree. The old tree is never modified and stays as a backup.

## Setup
- Mount the old library at `/import-source` **read-only** in compose.
- The new library lives at `/data/library`. It may sit on the same disk, but the files are copies.
- **No hardlinks.** A tag write on a hardlinked file would also change the original.

## Preflight (the import refuses to start if any check fails)
1. Walk `/import-source`: count files, total the size, and list unreadable files.
2. **Free space on `/data/library` must be at least 1.1× the source size.**
3. Show an ETA (a lower bound: MusicBrainz requests plus one AcoustID lookup per file, at the [service limits](services-etiquette.md)).
4. Show a summary and require confirmation: the "other MusicBrainz clients paused" checklist item ([why](services-etiquette.md#musicbrainz)) and, above 50 albums, the album count typed in.

## Flow
- One job per album folder (or per loose-file group), resumable through the job table.
- Every file gets an AcoustID lookup ([ADR 0009](../decisions/0009-acoustid-mandatory.md)).
- Each folder goes through [stage 2 matching](matching.md#stage-2-post-download-verification) with the admin's matching profile:
  - auto-accept → imported and canonicalized
  - review → review queue
  - no MB match → review queue with **Mark unverified** offered
- Album jobs run at lower priority than normal requests. For large libraries, consider a local MusicBrainz mirror (`MIXSYNC_MB_BASE_URL`).
- An album tagged with a `musicbrainz_albumid` is scored against that release (an unknown id falls back to search); otherwise against the top 3 search hits. A reject band or no candidate goes to review, where **Mark unverified**, pick another release, or reject (skips the album) are offered.
- WAV and AIFF are encoded to FLAC on import (poor player tagging support); Opus is tagged in place. Formats mixsync cannot take are listed as `skipped` (`unsupported format`). Extensions live in `core/audio.py`.
- The source tree is only read: never moved, written, or released. **Check old tree** re-hashes it against the preflight manifest.

## Report
- Totals: imported, in review, unverified, failed (with reasons).
- Downloadable as CSV.
- Exit criterion: the old tree is byte-identical before and after (checked by a hash manifest taken during preflight).
