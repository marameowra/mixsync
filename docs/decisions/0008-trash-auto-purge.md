# ADR 0008: Trash purges automatically, with no confirm step

- **Status:** Accepted
- **Date:** 2026-10-12

## Context
"Delete" moves files to `/data/.trash/<day>/`. Something has to empty it. The data-safety rules otherwise require a dry-run plan and confirmation for bulk operations, so a daily purge without one was a deliberate exception.

## Decision
- A daily job purges trash whose day folder is older than `MIXSYNC_TRASH_DAYS` (default 30). Each file is a journaled `purge` op. There is no dry-run or confirm step.
- Trash that undo still needs is kept: plans that can still be undone, and the trashed original of any op not yet done. One helper decides, so purge and undo cannot disagree. The rules are in [data safety](../design/data-safety.md#1-no-hard-deletes-in-the-normal-flow).
- Undo refuses once its window has passed; raising `MIXSYNC_TRASH_DAYS` later does not bring purged files back.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Automatic, journaled (chosen)** | Disk does not fill silently; no chore for the user | A mistake is unrecoverable after the window |
| Purge only on user confirmation | Maximum caution | Trash grows forever; nobody confirms |

## Consequences
- The only automatic delete in the library besides a verified `release` from `/downloads`.
- Every purge is in the journal, so it is auditable even though it is not previewed.
