# Data safety

Non-negotiable rules. Any code that touches files or the library goes through `library/fileops.py`, and `fileops.py` enforces these rules.

## 1. No hard deletes in the normal flow
- "Delete" means **move to `/data/.trash/YYYY-MM-DD/<original relative path>`**.
- Trash is purged after N days (default 30). The purge is itself a journaled job.
- All trash (library entries and `.discover/`) is purged automatically by a daily job once its `<day>` folder is older than `MIXSYNC_TRASH_DAYS` (default 30; a folder exactly that old is kept). Each file is a journaled `purge` op (`fileops.purge_trash`). There is no dry-run or confirm step (user decision 2026-10-12). Age is the day folder's name, not file mtimes. Exception: entries trashed by a merge or promote plan that can still be undone are kept, because undo restores from the trash. A plan is undoable while running or undoing, and for `MIXSYNC_TRASH_DAYS` days after it finished (done or failed; measured from its last change). After that the Undo button is hidden, `undo_plan` refuses ("undo window of N days has passed"), and the trash is purged. One helper (`db/undo.py: undoable`) decides, so the purge and the undo never disagree.
- Purging early from the UI requires a typed confirmation.
- One exception: a **released download**. After an import with zero errors, `fileops.release()` hard-deletes each source file from `/downloads` (journaled, and refused unless the path is strictly inside the downloads dir with no symlink or `..` escape). `/downloads` is staging, not library data; the library copy is hash-verified first; and `/downloads` and `/data` are separate mounts, so a "trash" move would be a second full copy.

## 2. Atomic, verified imports
1. Copy the source to a temp file **on the same filesystem as the destination** (`/data/.incoming/`).
2. `fsync` the file and its directory.
3. Verify the hash (BLAKE3 or SHA-256) of the copy against the source.
4. Write tags to the temp copy, after taking a tag snapshot (rule 3).
5. Atomically `rename` it to the final path. If the destination exists, the import aborts; it never overwrites.
6. Only then release the source: `release()` re-checks that the library copy still matches the hash journaled for this batch, journals the release, deletes the download, and removes its folder if empty. Then the pipeline clears the transfers from slskd's list. A partial failure releases nothing.

A crash at any step leaves either the old state or the new state, never a partial file in the library.

## 3. Tag snapshots
- Before any tag write, the full original tag set is serialized and stored in the DB, keyed by file and operation.
- Any retag can be undone from the UI.

## 4. Operation journal
- Every file operation is recorded: `op_id`, `batch_id`, `kind` (copy / move / trash / release / retag), `from`, `to`, `hash`, `started_at`, `finished_at`, `status`.
- Each op also records its `root`: `library` (`/data/library`) or `discover` (`/data/discover`, a second Navidrome library). `recover()` resolves relative paths against the op's root. Discover trash goes to `/data/.trash/YYYY-MM-DD/.discover/<rel>` (a prefix no sanitized library path can have, since segments never start with a dot). Discover must be on the same filesystem as `.incoming` and `.trash`, otherwise every discover import fails safely with EXDEV.
- A batch (merge, reorganize, migration) can be undone as a unit, replayed in reverse.
- On startup, the worker scans for ops that were started but not finished and either completes or rolls back each one.

## 5. Dry-run and confirm
- Every bulk operation (merge, reorganize, template change, canonical release change) shows a **dry-run plan** first: counts plus a list of every move or retag. The automatic trash purge (rule 1) is the exception: journaled, but with no plan.
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
