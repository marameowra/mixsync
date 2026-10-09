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
- Each folder goes through [stage 2 matching](matching.md#stage-2-post-download-verification) with the admin's matching profile:
  - auto-accept → imported and canonicalized
  - review → review queue
  - no MB match → review queue with **Mark unverified** offered
- Throughput is limited by the [service limits](services-etiquette.md).
- MusicBrainz's 1 req/s limit is **per IP**, so **pause other MusicBrainz clients on the network** (soulsync, Picard, beets, Lidarr) during migration. Otherwise both tools get refused. Preflight shows this as a checklist item.
- For large libraries, consider pointing MusicBrainz at a local mirror for the duration of the migration.

## Report
- Totals: imported, in review, unverified, failed (with reasons).
- Downloadable as CSV.
- Exit criterion: the old tree is byte-identical before and after (checked by a hash manifest taken during preflight).
