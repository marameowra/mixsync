# Data safety

Non-negotiable rules. Any code that touches files or the library goes through `library/fileops.py`, and `fileops.py` enforces these rules.

## 1. No hard deletes in the normal flow
- "Delete" means **move to `/data/.trash/YYYY-MM-DD/<original relative path>`** (discover files: `.trash/<day>/.discover/<rel>`).
- **Auto-purge** ([ADR 0008](../decisions/0008-trash-auto-purge.md)): a daily job deletes trash whose day folder is older than `MIXSYNC_TRASH_DAYS` (default 30; a folder exactly that old is kept). Each file is a journaled `purge` op. There is no dry-run or confirm step. Age is the folder name, not file mtimes.
- **Kept for undo:** trash belonging to plans that can still be undone, and the trashed original of any op not yet done (it may be the only copy). A done plan is undoable for `MIXSYNC_TRASH_DAYS` days after its last change, and its trash is purged one day later so the purge never races an undo. Failed, running and undoing plans are never purged. After the window the Undo button is hidden and `undo_plan` refuses.
- Raising `MIXSYNC_TRASH_DAYS` does not bring back trash already purged.
- Purging early from the UI requires a typed confirmation.
- **Released download:** after an import with zero errors, `fileops.release()` hard-deletes each source file from `/downloads` (journaled; refused unless strictly inside the downloads dir, no symlink or `..`). `/downloads` is staging, the library copy is hash-verified first, and a "trash" move across the two mounts would be a second full copy.
- **Quarantine** (`/data/quarantine/<rejected|review>/<YYYY-MM-DD>/<folder>/<file>`, [ADR 0007](../decisions/0007-quarantine-owned-by-mixsync.md)): MixSync-owned, so slskd retention cannot touch it.
  - A dropped candidate's files are copied there (journaled, hash-verified), then the `/downloads` source is released. Verify rejection or an unusable file goes to `rejected/`; a review-band match goes to `review/`. Evidence and assignment paths point at the quarantine copies.
  - Accepting a review candidate imports from quarantine and then releases the quarantine copy like a released download. A person's reject leaves the files in place; the decision's `final_action = reject` and `acted_at` mark them rejected.
  - Migration, drop-folder and adopted files are never quarantined, nor are rejected folders already in `/downloads`. The adopter scans only `/downloads`.
  - MixSync deletes nothing else from quarantine.

> Planned — not built (#208): a journaled purge of rejected quarantine entries (`rejected/<day>` aged by folder name; `review/` entries a person rejected, aged by `acted_at`; other `review/` entries never).

## Managed paths
- Library, discover, staging, trash, quarantine and scratch paths reject absolute paths, `..`, and symlinked components, including the managed root itself. Resolved destinations must stay under that root; missing destination directories are allowed. Ancestor symlinks above a configured root (for example a data mount) are allowed.
- Recovery and verification apply the same checks to journaled paths. A rejected pending operation stays pending for inspection; files are not moved or deleted through it.
- These checks reject existing symlinks; they do not protect against another process replacing a directory between validation and the filesystem operation.

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
- Any retag can be undone from the UI: a track's "Restore earlier tags" page (reached from the unverified list and a done plan's moves) previews the managed tags against the newest snapshot for its path and, on confirm, trashes the file and copies it back with those tags (journaled, verified), after first snapshotting the current tags so the restore can itself be undone. File location and MusicBrainz ids in the database are not changed.

## 4. Operation journal
- Every file operation is recorded: `op_id`, `batch_id`, `kind` (`copy` / `trash` / `release` / `restore` / `purge`; the `JournalOp` kinds in `core/protocols.py`), `from`, `to`, `hash`, `started_at`, `finished_at`, `status`.
- Each op also records its `root`: `library` (`/data/library`) or `discover` (`/data/discover`, a second Navidrome library). `recover()` resolves relative paths against the op's root. Discover trash goes to `/data/.trash/YYYY-MM-DD/.discover/<rel>` (a prefix no sanitized library path can have, since segments never start with a dot). Discover must be on the same filesystem as `.incoming` and `.trash`, otherwise every discover import fails safely with EXDEV.
- A batch (merge, reorganize, migration) can be undone as a unit, replayed in reverse.
- On startup, the worker scans for ops that were started but not finished and either completes or rolls back each one.

## 5. Dry-run and confirm
- Every bulk operation (merge, reorganize, template change, canonical release change) shows a **dry-run plan** first: counts plus a list of every move or retag. The automatic trash purge (rule 1) is the exception: journaled, but with no plan.
- Above a threshold (default 50 files), confirmation needs the count typed in.
- No bulk operation runs as a side effect of a settings change.

## 6. Backups
> Planned — not built (#222): nightly `VACUUM INTO /config/backups/mixsync-YYYY-MM-DD.db` keeping 7 copies, and a pre-migration DB backup.

Settings export/import as a single file is likewise not built.

## 7. Independence
- The library is plain, standard-tagged audio files. Navidrome or any player works with it even if MixSync is gone.
- MixSync never writes proprietary formats into the library. Its own markers are plain text tags (e.g. `MIXSYNC_STATUS`).

## Read-only mounts
Anything MixSync must not modify (for example the old library during [migration](migration.md)) is mounted **read-only** in compose. That is a guarantee from the operating system, not a code convention.
