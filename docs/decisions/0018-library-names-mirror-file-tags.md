# ADR 0018: Library name columns mirror the file's album tags

- **Status:** Accepted
- **Date:** 2026-11-03

## Context
Browsing the library (#266) needs every album's album artist and title, sortable and searchable, without calling MusicBrainz on a list page ([ADR 0011](0011-musicbrainz-ids-and-key-fields-only.md)). `tracks` stored only MBIDs. Names came from match evidence and requests, so an album with neither, such as one imported by migration, had no name. Unverified albums were grouped by reading the tags of every unverified file on each request.

## Decision
- `tracks` stores the album artist, album and year **as written in the file's tags**.
- Every path that writes tags (import, merge and promote plans, tag restore, and their undo) updates these columns in the same transaction as its journaled file operation.
- Both the browse page and the unverified list group albums from these columns, not by reading files.
- Existing rows are filled by a background job that only reads tags. Until it finishes, unfilled tracks show as "names pending".

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Columns mirror file tags (chosen)** | One source of truth that matches what the media server shows; covers unverified and migrated albums | Every tag writer must keep the columns in sync |
| Names from the MusicBrainz match (evidence, requests, cache) | No sync with tag writes | Missing for unverified and some migrated albums; disagrees with the files after a tag restore; can't be searched in SQL |
| Read tags when the page loads | No schema change | Slow on large libraries; no SQL sort or search |

## Consequences
- A new tag-writing path that skips the column update makes the browse page show stale names. Tests for tag writers check the columns.
- The backfill reads every library file once, as a job with progress, not inside the schema migration.
