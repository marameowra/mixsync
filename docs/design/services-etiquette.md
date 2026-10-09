# Respecting external services

MixSync uses free, community-run services. It must use them as intended and never hammer them.

## Shared rate limiter
- One **token bucket per service**, shared by all workers. State lives in the DB, so separate worker containers respect the same limits.
- Every outbound request goes through the limiter. There is no bypass.
- `429` and `503` responses back off exponentially and honor `Retry-After`.

| Service | Limit | Identity |
|---|---|---|
| MusicBrainz | 1 req/s | Descriptive User-Agent: `MixSync/<version> ( <contact URL or email> )` |
| Cover Art Archive | 1 req/s (conservative) | Same User-Agent |
| AcoustID | ≤ 3 req/s | Application API key; user key for submissions |
| ListenBrainz | Honor the `X-RateLimit-*` response headers | User token |
| Last.fm | ≤ 5 req/s (conservative) | Application API key |

**User-Agent contact (decided):** defaults to the project's GitHub URL, so the header reads `MixSync/<version> ( https://github.com/marameowra/mixsync )`. Each install can override it with `MIXSYNC_MB_CONTACT`.

## Caching
- Persistent on-disk response cache for MB, CAA, and AcoustID lookups.
- MB entity data is cached for 7 days by default. Search results are cached for 1 day.
- An optional **local MusicBrainz mirror** (musicbrainz-docker) can replace the public API, which is useful for large migrations.

## Soulseek
- **Share back.** The library is shared through slskd (configurable folders). Downloading without sharing goes against Soulseek norms.
- Cap concurrent searches (default 2). Duplicate searches are debounced.
- Failed searches back off exponentially before retrying (1 h → 6 h → 24 h → weekly).
- Respect peer queues; don't queue many files from a single peer at once.

## Jittered background jobs
The watchlist poll, unverified re-checks, and discovery refresh are spread randomly over their window, so jobs don't all fire at the same moment.
