# ADR 0015: Watchlist weekly cap defaults

- **Status:** Accepted
- **Date:** 2026-10-20

## Context
Following a label can produce dozens of releases a week; following an artist produces a handful a year. One default for both would either flood the queue or throttle artists.

## Decision
`weekly_cap` defaults to 5 for labels and unlimited for artists, and can be set per follow. Hits over the cap are recorded as `skipped_cap` and shown for manual request. See the [watchlist design](../design/watchlist.md).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Per-type defaults (chosen)** | Labels bounded, artists unrestricted | Two defaults to document |
| One default for both | Simple | Too loose for labels or too tight for artists |

## Consequences
- A label follow never queues more than 5 albums a week without the user asking.
