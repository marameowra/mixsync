# ADR 0011: Domain tables keep MusicBrainz ids and key fields only

- **Status:** Accepted
- **Date:** 2026-10-20

## Context
MusicBrainz responses are large (tracklists, labels, relationships) and change upstream. Copying them into domain tables would create a second copy to keep fresh. MusicBrainz also limits us to 1 request per second ([services etiquette](../design/services-etiquette.md)).

## Decision
- `releases` and `release_groups` store the MBID plus only the fields MixSync filters or sorts on: title, type, country, date, media format, track count, status. That is enough for the [canonical release policy](../design/library.md#release-group-canonicalization).
- Full responses live only in the server-side HTTP cache in `/config`, with expiry dates. They are never copied into domain tables.
- Library pages read from `tracks` and never call MusicBrainz.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Ids + key fields (chosen)** | Small tables; one source of truth upstream; no refresh logic | Cold-cache reports refetch |
| Mirror full MB data in tables | Offline, fast reports | A stale copy to maintain; large schema |

## Consequences
- A cold fragmentation report refetches one release group per second, about 33 minutes per 2,000 groups. It runs as a background job with progress.
- Adding a filterable field means a migration plus a backfill from the cache or MusicBrainz.
