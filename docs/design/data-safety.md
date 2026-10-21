# Data safety

Non-negotiable rules. Code that touches files or the library goes through `library/fileops.py`, which enforces them.

## 1. No hard deletes in the normal flow
- "Delete" moves to `/data/.trash/YYYY-MM-DD/<original relative path>` (discover files under `<day>/.discover/`).
- **Auto-purge** ([ADR 0008](../decisions/0008-trash-auto-purge.md)): a daily job deletes trash whose day folder is older than `MIXSYNC_TRASH_DAYS` (default 30; exactly that old is kept). Each file is a journaled `purge` op; no dry-run or confirm. Age is the folder name, not mtime.
- **Kept for undo:** trash of plans that can still be undone, and the trashed original of any op not yet done (maybe the only copy). A done plan is undoable for `MIXSYNC_TRASH_DAYS` days after its last change; its trash is purged one day later so purge never races undo. Failed, running and undoing plans are never purged. After the window, undo refuses and the button is hidden.
- Raising `MIXSYNC_TRASH_DAYS` does not restore purged trash. Purging early from the UI needs a typed confirmation.
- **Released download:** after an import with zero errors, `fileops.release()` hard-deletes each source from `/downloads` (journaled; refused unless strictly inside the downloads dir, no symlink or `..`). `/downloads` is staging, the library copy is hash-verified first, and a trash move across the two mounts would be a second full copy.
- **Quarantine** (`/data/quarantine/<rejected|review>/<YYYY-MM-DD>/<folder>/<file>`, [ADR 0007](../decisions/0007-quarantine-owned-by-mixsync.md)) is MixSync-owned, out of slskd retention's reach.
  - A dropped candidate's files are copied there (journaled, hash-verified), then the `/downloads` source is released. Rejections and unusable files go to `rejected/`, review-band matches to `review/`; evidence and assignment paths point at the quarantine copies.
  - Accepting a review candidate imports from quarantine, then releases the quarantine copy like a released download. A person's reject sets the decision's `final_action = reject` and `acted_at` and queues a `refile` job: it moves the decision's files from `review/` into `rejected/<today>/` (verified copy, then journaled release, root `quarantine`; safe to repeat) and points the decision's evidence paths at the new copies. Files not under `review/` stay where they are. Candidates rejected before this existed stay in `review/` (no bulk migration).
  - Migration, drop-folder and adopted files are never quarantined, nor are rejected folders already in `/downloads`.
  - **Auto-purge:** the same daily job deletes `rejected/<day>` folders older than `MIXSYNC_TRASH_DAYS` (by folder name; exactly that old is kept). Each file is a journaled `purge` op with root `quarantine`; symlinks are skipped. `review/` is never purged: a person's reject re-files its files into `rejected/` first.
  - Nothing else is deleted from quarantine.

## Managed paths
- Library, discover, staging, trash, quarantine and scratch paths reject absolute paths, `..`, and symlinked components, including the root itself. Destinations must resolve under the root; missing destination directories are fine. Ancestor symlinks above a configured root (a data mount) are fine.
- Recovery and verification apply the same checks to journaled paths. A rejected pending op stays pending for inspection and nothing is moved through it.
- Not protected: another process swapping a directory between validation and the operation.

## 2. Atomic, verified imports
1. Copy to a temp file on the destination's filesystem (`/data/.incoming/`).
2. `fsync` the file and its directory.
3. Verify the hash of the copy against the source.
4. Write tags to the temp copy after a tag snapshot (rule 3).
5. Atomically `rename` into place. An existing destination aborts the import; never overwrite.
6. Release the source: `release()` re-checks the library copy against the journaled hash, journals the release, deletes the download, removes its folder if empty, then clears the transfers from slskd. A partial failure releases nothing.

A crash at any step leaves the old or the new state, never a partial file in the library.

## 3. Tag snapshots
Before any tag write, the full original tag set is stored in the DB, keyed by file and operation. A track's "Restore earlier tags" page previews the managed tags against the newest snapshot and, on confirm, trashes the file and copies it back with those tags (journaled, verified), snapshotting the current tags first so the restore is itself undoable. DB location and MusicBrainz ids are unchanged.

## 4. Operation journal
- Every file op records `op_id`, `batch_id`, `kind` (`copy` / `trash` / `release` / `restore` / `purge`; see `JournalOp` in `core/protocols.py`), `from`, `to`, hashes, timestamps, `status`, and `root` (`library` or `discover`; `recover()` resolves paths against it).
- Discover must share a filesystem with `.incoming` and `.trash`, otherwise discover imports fail safely with EXDEV.
- A batch (merge, reorganize, migration) can be undone as a unit, replayed in reverse.
- On startup the worker finds ops started but not finished and completes or rolls back each.

## 5. Dry-run and confirm
- Every bulk operation (merge, reorganize, template change, canonical release change) shows a dry-run plan first: counts and every move or retag. The auto-purge (rule 1) is the only exception.
- Above 50 files, confirmation needs the count typed in.
- No bulk operation runs as a side effect of a settings change.

## 6. Backups
- SQLite: a nightly `VACUUM INTO <backup dir>/mixsync-YYYY-MM-DD.db` (UTC date) keeps the 7 newest `mixsync-*.db` files and touches nothing else there. The file is written under a temporary name and renamed on success, so a half-written file never looks like a backup. `mixsync backup` takes one now.
- `mixsync migrate` backs up first when the database has a revision and is behind head; if the backup fails the upgrade does not run. A fresh database or one at head is not backed up.
- The backup dir is `MIXSYNC_BACKUP_DIR` (default `/config/backups`).
- Postgres: no backups here. `mixsync backup` exits non-zero and says to use `pg_dump`; the nightly job does nothing; `migrate` logs a notice and upgrades.
- Restore is manual: stop MixSync, copy a backup over the database file.

> Planned — not built: settings export/import.

## 7. Independence
The library is plain, standard-tagged audio. Navidrome or any player works without MixSync. Its own markers are plain text tags (e.g. `MIXSYNC_STATUS`).

## Read-only mounts
Anything MixSync must not modify (the old library during [migration](migration.md)) is mounted read-only in compose: an OS guarantee, not a code convention.
