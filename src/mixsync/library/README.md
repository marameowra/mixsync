# `library`

## Purpose
Everything that touches the music library on disk: where files go, how they get there safely, how they are tagged, and which release they belong to.

## Owns / does not own
- **Owns:** path templates (`paths.py`), **all filesystem writes under `/data`** (`fileops.py`), tags (`tags.py`, `tag_undo.py`), canonical release policy (`canonical.py`), the importer, trash purge (`purge.py`), the fragmentation report (`fragmentation.py`).
- **Does not own:** scoring (`match`), downloading (`sources`), or notifying Navidrome (`targets`, called by the composition root after import). Merge execution and undo live in `acquire/merge.py`.

## Rules / invariants
- `fileops.import_file` follows the journaled sequence in [data safety](../../../docs/design/data-safety.md). On startup the worker calls `fileops.recover()`: each pending op is completed or rolled back.
- **No `os.remove` / `unlink` / `shutil.rmtree` outside `purge`**, which only acts on `/data/.trash` entries past `MIXSYNC_TRASH_DAYS`. Enforced by ruff `TID251`; because it cannot see `Path(...).unlink()`, `test_nothing_in_src_deletes` greps for it too. Rollback and trash only ever move files into `/data/.trash`.
- `rename` stays within `/data` (one mount). A cross-device error (`EXDEV`) is a `SafetyError`, never a copy+delete fallback.
- The final rename must fail if the destination exists (no overwrite).
- The purge skips trash entries of any plan that can still be undone, and the trashed original of any not-done move/trash op. It refuses to run through a symlinked `.trash`.
- Every bulk change returns a `Plan` first; executing one above 50 ops needs the typed count from the UI.
- Paths in the DB are relative to the library root.
- Changing the path template never moves files by itself; it produces a reorganize `Plan`. Default template and genre source: [ADR 0014](../../../docs/decisions/0014-default-path-template-and-genres.md).

## May import
`core`, `db`, `match`.

## Design doc
[Data safety](../../../docs/design/data-safety.md) · [Library layout and canonicalization](../../../docs/design/library.md)
