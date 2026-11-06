# ADR 0019: Each track records the path template it was filed with

- **Status:** Accepted
- **Date:** 2026-11-06

## Context
The path template is one global setting (`MIXSYNC_PATH_TEMPLATE`) that only new imports read, so a change leaves the library split between two layouts (#285). Data safety rule 5 forbids moving files as a side effect of a settings change, so a change must produce a dry-run reorganize plan the user confirms. That needs three choices: how to detect files filed under another template, how much one plan covers, and what starts it.

## Decision
- **Drift detection:** `tracks.path_template` stores the template each file's path was rendered with. Every writer of `tracks.path` sets it in the same transaction: the importer, merge and promote moves, reorganize moves, and their undo (which puts the old value back). Drift is a SQL count of rows whose template differs from the setting; no file is read. Rows from before this column are labelled with the configured template by the worker at startup; a null row is never counted as drift.
- **Scope:** one `reorganize` plan for the whole library (`release_group_mbid = ""`), one typed confirmation above 50 moves, one undo batch. The plan page shows the move count and moves grouped by album folder with a sample per folder, so it stays bounded on a large library.
- **Trigger:** the library page shows "Layout differs from the path template (N files)" with a "Plan reorganize" button. A plan is built only when the user clicks. Nothing at startup builds a plan.
- A reorganize move renders the new path from the file's current tags and does not change them: the copy keeps the original's bytes. A move onto another filesystem is listed as refused, never copied across.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Template stored per track (chosen)** | Cheap SQL drift count; a tag edit after import is not drift | A migration, a startup backfill, and every path writer must set it |
| Recompute each path from tags and the current template | No new state | Reads every file to show a banner; any tag edit looks like drift |
| **One plan for the whole library (chosen)** | One confirmation and one undo for one settings change | A large plan page; bounded by grouping and sampling |
| One plan per artist or album | Each confirmation is small | Many plans to confirm and undo for one change; partial layouts in between |
| **Banner and button (chosen)** | Nothing reads tags or builds plans unasked | The user has to click |
| Build a plan at worker startup | The plan is ready when the user looks | Startup work after every restart; a plan the user never asked for |

## Consequences
- A new path writer that skips `path_template` makes its files look drifted (or hides their drift). Tests for path writers check the column.
- Files already off-template when this shipped are labelled with the current template by the backfill, so they are not reported. Changing the template after that is detected.
- A refused or unreadable file stays drifted, so the banner stays until it is fixed by hand.
- A plan is stale once the template or any of its track rows changes: execution refuses and nothing moves.
