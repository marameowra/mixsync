# `library`

## Purpose
Everything that touches the music library on disk: where files go, how they get there safely, how they are tagged, and which release they belong to.

## Owns / does not own
- **Owns:** path templates, **all filesystem writes under `/data`**, tag reading/writing, canonical release selection, the importer pipeline, trash, the fragmentation report.
- **Does not own:** scoring (`match`), downloading (`sources`), or notifying Navidrome (`targets`, called by the composition root after import).

## Proposed files
| File | Contents |
|---|---|
| `paths.py` | Template rendering + sanitization; collision suffixes |
| `fileops.py` | **The only module that writes files**: `import_file`, `move`, `trash`, `purge`, `restore` |
| `tags.py` | mutagen read/write, standard MusicBrainz tag names (Picard-compatible), snapshot before write, `MIXSYNC_STATUS` |
| `canonical.py` | Canonical release policy evaluation; the recording → canonical track mapping; bonus-track handling |
| `importer.py` | Orchestrates: verified match → canonical release → path → `fileops.import_file` → DB `tracks` row |
| `fragmentation.py` | Finds release groups split across releases; builds a dry-run merge plan |
| `plans.py` | `Plan` type for dry-runs (list of ops + counts), executed only after confirmation |

## `fileops.import_file` sequence (must match the design doc exactly)
```
1. journal.begin(copy, src, /data/.incoming/<op_id>)
2. copy → fsync file → fsync dir
3. hash copy == hash src, else fail (src untouched)
4. tags.snapshot(); tags.write(copy)
5. rename copy → final path        # fails if the destination exists; never overwrites
6. journal.complete(op_id, hash)
7. caller releases the source (sources.release)
```
On startup, `worker.py` calls `fileops.recover()`: each pending op is either completed or rolled back.

## Rules
- **No `os.remove` / `unlink` / `shutil.rmtree` anywhere except `purge()`**, which only acts on `/data/.trash` entries past retention. Enforce with a ruff banned-API rule.
- Every bulk change returns a `Plan` first. Executing a plan above 50 ops requires the typed count from the UI.
- `rename` only within `/data` (one mount). A cross-device error (`EXDEV`) is a `SafetyError`, never a silent copy+delete fallback.
- Paths in the DB are relative to the library root.
- Changing a template never moves files by itself; it produces a reorganize `Plan`.

**Decided:** the default template is `{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}`. `{genre}` comes from MusicBrainz genres (release group → release → artist), top 1 for paths and top 3 for tags. Details: [library design](../../../docs/design/library.md#path-templates).

## May import from
`core`, `db`, `match`. (Uses mutagen, blake3/hashlib.)

## Tests
- Hypothesis property tests for `paths.py`: no collisions, no illegal characters, deterministic output.
- **Crash-safety:** SIGKILL at each step of `import_file` → no partial file in `/data/library`, source intact, recovery completes ([testing](../../../docs/testing.md#crash-safety-tests)).
- Canonical policy ordering; bonus-track filing; fragmentation plan correctness.

## Design docs
[Data safety](../../../docs/design/data-safety.md) · [Library layout and canonicalization](../../../docs/design/library.md)
