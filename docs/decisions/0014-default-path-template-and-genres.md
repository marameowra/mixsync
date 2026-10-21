# ADR 0014: Default path template and genre source

- **Status:** Accepted
- **Date:** 2026-10-20

## Context
The library layout must be predictable for Navidrome, Plex and Jellyfin, and stable enough that files rarely move.

## Decision
- Default template: `{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}`.
- `{genre}` comes from MusicBrainz's curated genres (release group, then release, then artist): top 1 for paths, top 3 for tags. Folksonomy `tags` are never used.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Curated genres (chosen)** | Few, stable values, so paths do not churn | Fewer genres per release |
| Folksonomy tags | More coverage | Noisy and unstable |

Template syntax and sanitization are in the [library design](../design/library.md#path-templates).

## Consequences
- Changing the template never moves files by itself; it produces a reorganize plan ([data safety](../design/data-safety.md)).
