# Data safety

Non-negotiable rules. Any code that touches files or the library goes through `library/fileops.py`, and `fileops.py` enforces these rules.

## 1. No hard deletes in the normal flow
- "Delete" means **move to `/data/.trash/YYYY-MM-DD/<original relative path>`**.
- Trash is purged after N days (default 30). The purge is itself a journaled job.
- Purging early from the UI requires a typed confirmation.

## 2. Atomic, verified imports
1. Copy the source to a temp file **on the same filesystem as the destination** (`/data/.incoming/`).
2. `fsync` the file and its directory.
3. Verify the hash (BLAKE3 or SHA-256) of the copy against the source.
4. Write tags to the temp copy, after taking a tag snapshot (rule 3).
5. Atomically `rename` it to the final path. If the destination exists, the import aborts; it never overwrites.
6. Only then mark the source as released, so slskd can clean it up.

A crash at any step leaves either the old state or the new state, never a partial file in the library.

## 3. Tag snapshots
- Before any tag write, the full original tag set is serialized and stored in the DB, keyed by file and operation.
- Any retag can be undone from the UI.

## 4. Operation journal
- Every file operation is recorded: `op_id`, `batch_id`, `kind` (copy / move / trash / retag), `from`, `to`, `hash`, `started_at`, `finished_at`, `status`.
- A batch (merge, reorganize, migration) can be undone as a unit, replayed in reverse.
- On startup, the worker scans for ops that were started but not finished and either completes or rolls back each one.

## 5. Dry-run and confirm
- Every bulk operation (merge, reorganize, purge, template change, canonical release change) shows a **dry-run plan** first: counts plus a list of every move or retag.
- Above a threshold (default 50 files), confirmation needs the count typed in.
- No bulk operation runs as a side effect of a settings change.

## 6. Backups
- Nightly `VACUUM INTO /config/backups/mixsync-YYYY-MM-DD.db`, keeping 7 copies.
- Settings can be exported and imported as a single file.

## 7. Independence
- The library is plain, standard-tagged audio files. Navidrome or any player works with it even if MixSync is gone.
- MixSync never writes proprietary formats into the library. Its own markers are plain text tags (e.g. `MIXSYNC_STATUS`).

## Read-only mounts
Anything MixSync must not modify (for example the old library during [migration](migration.md)) is mounted **read-only** in compose. That is a guarantee from the operating system, not a code convention.
