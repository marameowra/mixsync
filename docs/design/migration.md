# Migration from an existing library

**Approach:** re-import. The old library is copied through the matcher into a new tree. The old tree is never modified and stays as a backup.

## Setup
- Mount the old library at `/import-source` **read-only** in compose.
- The new library lives at `/data/library`. It may sit on the same disk, but the files are copies.
- **No hardlinks.** A tag write on a hardlinked file would also change the original.

## Preflight (the import refuses to start if any check fails)
1. Walk `/import-source`: count files, total the size, and list unreadable files.
2. **Free space on `/data/library` must be at least 1.1× the source size.**
3. Confirm the MB rate limit and estimated duration (about 1 MB lookup per album, plus 1 AcoustID lookup per track at ≤3/s). Show an ETA before starting.
4. Show a summary and require confirmation.

## Flow
- One job per album folder (or per loose-file group), resumable through the job table.
- **Every file gets an AcoustID lookup**, even if it already has MusicBrainz tags (decided). The preflight ETA includes all of them.
- Each folder goes through [stage 2 matching](matching.md#stage-2-post-download-verification) with the admin's matching profile:
  - auto-accept → imported and canonicalized
  - review → review queue
  - no MB match → review queue with **Mark unverified** offered
- Throughput is limited by the [service limits](services-etiquette.md).
- MusicBrainz's 1 req/s limit is **per IP**, so **pause other MusicBrainz clients on the network** (soulsync, Picard, beets, Lidarr) during migration. Otherwise both tools get refused. Preflight shows this as a checklist item.
- For large libraries, consider pointing MusicBrainz at a local mirror for the duration of the migration.

## As built
- Admin page `/migration`. Preflight hashes every regular file into `<config>/migrations/<id>/manifest.jsonl` and groups the audio files (mp3, flac, ogg, opus, m4a, wav) into albums (a disc subfolder joins its album folder; loose files split by album tag). Unreadable files are listed, left out, and appear as failed in the report. Audio in a format mixsync cannot take (wma, aac, ape, wv, aiff...) is listed the same way and appears as `skipped` with the reason `unsupported format`.
- **Opus** is tagged in place through Vorbis comments, like ogg. **WAV** is encoded to FLAC on import (WAV tagging is poorly supported by players), by the same path as downloads; the original WAV is only read, never moved or written, and the byte-identical check covers it. The supported extensions live in one place, `library/tags.py` (`EXTENSIONS`), shared with the import path.
- The ETA is the sum of the MusicBrainz requests (1 per album with a `musicbrainz_albumid` tag, 4 without) and the AcoustID lookups, and is a lower bound.
- Confirming needs the "other MusicBrainz clients paused" box and, above 50 albums, the album count typed in. Album jobs run at lower priority than normal requests.
- An album with the tag is scored against that release (a tag MusicBrainz does not know falls back to the search); otherwise against the top 3 search hits. A reject band, or no candidate, goes to review (Mark unverified, pick another release, or reject, which skips the album). The source files are never released, moved or written.
- "Check old tree" re-hashes the source against the manifest.

## Report
- Totals: imported, in review, unverified, failed (with reasons).
- Downloadable as CSV.
- Exit criterion: the old tree is byte-identical before and after (checked by a hash manifest taken during preflight).
