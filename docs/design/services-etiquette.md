# Respecting external services

Use free, community-run services as intended; never hammer them. Rules below were checked against each service's docs on 2026-10-09. Re-check before each release.

## Shared rules
- One token bucket per service, shared by all workers (state in the DB). Every outbound request goes through it; no bypass. Code: `src/mixsync/ratelimit/`.
- `429`/`503`: honor `Retry-After` or the service's reset header, else exponential backoff with jitter.
- Every request sends `User-Agent: MixSync/<version> ( https://github.com/marameowra/mixsync )`, never a library default. Contact override: `MIXSYNC_MB_CONTACT`.
- Periodic jobs (watchlist, re-checks, discovery refresh) are jittered across their whole interval, never at fixed clock times.
- Cache: persistent response cache for MB, CAA, AcoustID, ListenBrainz (MB entities 7 days, searches 1 day). A local MusicBrainz mirror (`MIXSYNC_MB_BASE_URL`) can replace the public API.

## Summary
| Service | Limit | MixSync bucket | Status |
|---|---|---|---|
| MusicBrainz | ~1 req/s per IP | 1 req/s (`MIXSYNC_MB_RATE`, max 1) | core |
| Cover Art Archive | none | 1 req/s | core |
| AcoustID | 3 req/s | 3 req/s | core |
| ListenBrainz | 1 req/s + `X-RateLimit-*` | 1 req/s + headers | core |
| Last.fm | unspecified | 1 req/s | deferred |
| Discogs | 60/min authed, 25/min not | 50/min | planned (#223) |
| Deezer | none published | 1 req/s | planned (#223) |
| Soulseek | 5 clients per IP | 1 slskd client | core |

## MusicBrainz
- Over the limit, *all* requests from the IP are refused (HTTP 503). The limit is per IP and shared with every MB client on the network (soulsync, Picard, beets, Lidarr): pause them during heavy work such as migration. `blocked_until` pauses all workers.
- Never send a blank or library-default UA (`python-httpx`); unit-tested.
- Don't poll for changes: the watchlist does one search per check ([watchlist](watchlist.md#poller)); unverified re-checks back off ([details](unverified-and-mb-submit.md#re-check-and-promote)).
- Always `fmt=json`. `inc=` returns at most 25 linked entities, so lists come from browse/search. Advance paging `offset` by items received.
- No MB API writes. Edits go through release-editor seeding in the browser.
- Non-commercial use only.

[Rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)

## Cover Art Archive
- No documented limit; self-imposed 1 req/s, 503 backs off.
- Image endpoints answer with 307 to archive.org: follow redirects (httpx doesn't by default), but never hot-link archive.org.
- Thumbnails are 250/500/1200 px: fetch 1200 for `cover.jpg`, 500 for the UI; on 404 fall back to the original, then the release group. Cache art on disk indefinitely.

## AcoustID
- Max 3 req/s. One app key per install (`MIXSYNC_ACOUSTID_APP_KEY`); never ship the public example key.
- One deployment-wide user key (`MIXSYNC_ACOUSTID_USER_KEY`, decided 2026-10-10): all submissions go under that account, no per-user storage. Without it (or the app key) submissions are skipped.
- Submissions: batch per album, gzip POST, indexed params, always include `mbid`, poll status a few times with backoff.
- Every file is looked up, no exceptions: [ADR 0009](../decisions/0009-acoustid-mandatory.md). For very large libraries the migration preflight suggests contacting AcoustID first.
- Non-commercial use only.

[AcoustID web service](https://acoustid.org/webservice)

## ListenBrainz
- Max 1 call/s. On `X-RateLimit-Remaining` 0, set `blocked_until` = now + `X-RateLimit-Reset-In`; 429 backs off the same way.
- Per-user token (`Authorization: Token ...`) from `user_links`.

[ListenBrainz API](https://listenbrainz.readthedocs.io/en/latest/users/api/index.html)

## Last.fm
**Deferred, not in v1; this section is the home for the deferral.** MixSync turns recommendations into Soulseek downloads, and Last.fm's terms (cl. 5.1.8, 4.7) exclude use alongside unauthorised sharing. If reconsidered: self-imposed 1 req/s, honor cache headers, cache only in the HTTP cache with a 100 MB cap, show "via Last.fm" with a link. [ToS](https://www.last.fm/api/tos)

## Discogs and Deezer (fallbacks, off by default)
> Planned — not built (#223).

- Discogs: 60 req/min authenticated, 25 unauthenticated, per IP over 60 s; bucket at 50/min; pause when `X-Discogs-Ratelimit-Remaining` hits 0; unique UA; per-user personal token. Numbers come from an OpenAPI copy (the official page blocks fetching): re-check by hand before enabling.
- Deezer: no quota; 1 req/s. Terms limit use to private family scope, non-commercial, so metadata suggestions only for a single-household install; never fetch audio. [Terms](https://developers.deezer.com/termsofuse)

## Soulseek
- At most 5 clients per IP: MixSync uses one slskd client. Bots must implement the full protocol: MixSync drives slskd, a full client, and never speaks the protocol itself.
- Share back through slskd (users who don't share get banned by peers). Sharing copyrighted material is the user's responsibility.
- Cap concurrent searches (default 2), debounce duplicates; failed searches back off 1 h → 6 h → 24 h → weekly.
- Don't queue many files from one peer at once.
- Cap concurrent downloads (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`, default 3, counting active transfers, not albums). A full cap defers a `download` job without spending an attempt. Slow transfers (`MIXSYNC_SLOW_DOWNLOAD_*`, see [deploy](../../deploy/README.md#environment-variables)) stop counting so a stalled peer cannot block the queue.

[Soulseek rules](https://www.slsknet.org/news/node/681)
