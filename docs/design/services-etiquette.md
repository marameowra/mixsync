# Respecting external services

MixSync uses free, mostly community-run services. It must use them as intended and never hammer them. Every rule below was checked against the service's own documentation on **2026-10-09**. Re-check these docs before each release, because limits change.

## Shared rate limiter
- One **token bucket per service**, shared by all workers. State lives in the DB, so separate worker containers respect the same limits.
- Every outbound request goes through the limiter. There is no bypass.
- `429` / `503` → honor `Retry-After` or the service's own reset header, else exponential backoff with jitter.
- Every request carries `User-Agent: MixSync/<version> ( https://github.com/marameowra/mixsync )`, even where a service doesn't require it. Override the contact with `MIXSYNC_MB_CONTACT`.

## Summary
| Service | Documented limit | MixSync setting | Identity | Status in v1 |
|---|---|---|---|---|
| MusicBrainz | ~1 req/s avg **per IP** | 1 req/s (≤ 1, configurable) | UA required | core |
| Cover Art Archive | none in place | 1 req/s (self-imposed) | UA (courtesy) | core |
| AcoustID | ≤ 3 req/s | 3 req/s | app key + per-user key | core |
| ListenBrainz | ≤ 1 req/s, plus `X-RateLimit-*` headers | 1 req/s + headers | UA required + user token | core (discovery) |
| Last.fm | no number; "several calls per second" continuously risks suspension | 1 req/s (self-imposed) | identifiable UA + API key | **off by default** (see the ToS risk below) |
| Discogs | 60/min authenticated, 25/min unauthenticated, per IP, rolling 60 s | 50/min | unique UA + user token | off by default (fallback) |
| Deezer | none published | 1 req/s (self-imposed) | n/a | off by default (fallback) |
| Soulseek | 5 clients per IP | 1 slskd client | Soulseek account | core |

## MusicBrainz
| Rule | What MusicBrainz says | What MixSync does |
|---|---|---|
| Rate | About 1 req/s average **per source IP**; over it, *all* requests from that IP are refused until the rate drops | Bucket at 1 req/s (`MIXSYNC_MB_RATE`, rejected if > 1). **Shared with every MB client on your network** (soulsync, Picard, beets, Lidarr), so pause those during heavy work like migration. |
| Refusal | HTTP **503**; no retry header documented | 503 → exponential backoff with jitter; `blocked_until` pauses all workers |
| User-Agent | Required: `Application name/<version> ( contact-url )` | Always set |
| Anonymous agents | A blank UA, `-`, `Python-urllib`, `Java`, etc. are throttled; some library default UAs share a cap | Never send a library default UA (`python-httpx`, `python-musicbrainz`). Unit-tested. |
| Scheduling | Don't run jobs at fixed clock times; spread background calls randomly | No fixed-hour schedules; every periodic job is jittered across its whole interval |
| Polling | Don't poll for metadata changes | Watchlist: one search per check with adaptive cadence ([watchlist](watchlist.md#poller)). Unverified re-checks back off and are mainly user-triggered ([details](unverified-and-mb-submit.md#re-check-and-promote)). |
| Format | XML by default; JSON via `fmt=json` or `Accept` | Always `fmt=json` |
| Lookups | `inc=` returns at most 25 linked entities | Lists come from browse/search, never a big `inc=` |
| Paging | Browse `limit` max 100; release browses capped at ~500 tracks | Advance `offset` by items *received* |
| Writes | OAuth; only tags, ratings, barcodes, ISRCs, collections via API | **No MB API writes.** Edits go through release-editor seeding in the browser. |
| Use | Non-commercial use is free | MixSync is non-commercial, self-hosted |

Sources: [Rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting) · [API overview](https://musicbrainz.org/doc/MusicBrainz_API) · [Search](https://musicbrainz.org/doc/MusicBrainz_API/Search)

## Cover Art Archive
| Rule | What the docs say | What MixSync does |
|---|---|---|
| Rate | "There are currently no rate limiting rules in place", though a 503 for exceeding a rate limit is listed | Self-imposed 1 req/s; 503 → backoff |
| Redirects | Image and listing endpoints answer with **307 redirects** to archive.org | The client **must follow redirects** (httpx doesn't by default) |
| Thumbnails | Supported sizes are 250, 500, and 1200 px; a redirected thumbnail may 404 | Fetch 1200 for `cover.jpg` and 500 for the UI; on 404, fall back to the original image, then to the release group |
| Access | All requests must go through coverartarchive.org | Never hot-link archive.org URLs directly |
| Caching | Filenames never change | Cache downloaded art on disk indefinitely (it's saved as `cover.jpg` anyway) |

Source: [Cover Art Archive API](https://musicbrainz.org/doc/Cover_Art_Archive/API)

## AcoustID
| Rule | What the docs say | What MixSync does |
|---|---|---|
| Rate | "Do not make more than 3 requests per second" | 3 req/s bucket |
| App key | Register the application; the key goes in `client` | One MixSync app key per install (`MIXSYNC_ACOUSTID_APP_KEY`). The public example key expires, so don't ship it. |
| User key | Needed for submissions; never store a key in app code, each user provides their own | Per-user key in `user_links` (encrypted) |
| Submissions | Batch with indexed params (`fingerprint.0`, …); gzip-compressed POST preferred; processed asynchronously | Batch per album; gzip POST; poll the submission status a few times with backoff; always include `mbid` |
| Heavy traffic | Tell the operators in advance if your app will generate significant traffic | Migration preflight shows the total AcoustID lookups. For very large libraries the preflight suggests contacting AcoustID first. |
| Use | Free for non-commercial use; must not be used with illegal products or services | Non-commercial |

Source: [AcoustID web service](https://acoustid.org/webservice)

> [!question] Fewer AcoustID lookups during migration
> Many files in an existing library already carry MusicBrainz recording IDs. Fingerprinting runs locally (`fpcalc`) and costs nothing. Proposal: still fingerprint everything, but call AcoustID only for files whose tags are missing, unresolvable, or disagree with the duration. That could cut lookups a lot, at the cost of trusting existing tags a little more.

## ListenBrainz
| Rule | What the docs say | What MixSync does |
|---|---|---|
| Rate | Never more than **one call per second**; limits are communicated by headers | 1 req/s bucket, tightened further by the headers |
| Headers | `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset-In` (recommended, clock-independent), `X-RateLimit-Reset` | When `Remaining` hits 0, set `blocked_until` = now + `Reset-In` |
| Refusal | 429 Too Many Requests | Backoff using `Reset-In` |
| User-Agent | Required, same format as MusicBrainz; requests without one may be blocked | Always set |
| Auth | `Authorization: Token <user token>`; authenticated requests may get higher limits | Per-user token from `user_links` |

Source: [ListenBrainz API](https://listenbrainz.readthedocs.io/en/latest/users/api/index.html)

## Last.fm
| Rule | What the docs say | What MixSync does |
|---|---|---|
| Rate | "Be reasonable"; continuously making several calls per second risks suspension; limits are at Last.fm's discretion | Self-imposed 1 req/s |
| User-Agent | Use an identifiable UA on all requests | Always set |
| Caching | Implement caching according to the response's HTTP headers | The cache honors `Cache-Control` / `Expires` for Last.fm |
| Storage cap | "Reasonable Usage Cap" of 100 MB of stored Last.fm data | Cache Last.fm only in the HTTP cache with a 100 MB size cap; never copy it into domain tables |
| Attribution | Credit Last.fm; link artist/album/track pages to Last.fm's catalogue | Discovery entries sourced from Last.fm show "via Last.fm" with a link |
| Use | Non-commercial only; the agreement excludes use with sites that facilitate unauthorised sharing (cl. 5.1.8) and unauthorised exploitation of IP (cl. 4.7) | See the risk note below |

Source: [Last.fm API ToS](https://www.last.fm/api/tos) · [API intro](https://www.last.fm/api/intro)

> [!question] Last.fm terms risk
> MixSync turns recommendations into Soulseek downloads. Last.fm's terms exclude use alongside unauthorised sharing, so using Last.fm data this way may breach them. Proposal: **Last.fm is off by default and ListenBrainz is the primary discovery source.** MetaBrainz runs ListenBrainz and publishes its data openly. Decide whether to keep Last.fm as an opt-in or drop it from v1.

## Discogs (fallback, off by default)
| Rule | What the docs say | What MixSync does |
|---|---|---|
| Rate | 60 req/min authenticated, 25/min unauthenticated, tracked **per source IP** over a moving 60 s window | 50/min bucket (headroom for other Discogs tools on the network) |
| Headers | `X-Discogs-Ratelimit`, `-Used`, `-Remaining` | Pause when `Remaining` hits 0 |
| User-Agent | A unique UA is required to get the full rate; generic ones are discouraged | Always set |
| Auth | Personal access token, key/secret, or OAuth 1.0a | Per-user personal token |

Source: Discogs API docs. The official page ([discogs.com/developers](https://www.discogs.com/developers)) blocked automated fetching, so these numbers come from a copy of the Discogs OpenAPI spec. Re-check them by hand before enabling Discogs.

## Deezer (fallback, off by default)
| Rule | What the terms say | What MixSync does |
|---|---|---|
| Rate | No published quota; Deezer may monitor usage | Self-imposed 1 req/s |
| Use | Content use is "limited to a strictly private use within a family scope", non-commercial | Allowed only as a tag-suggestion source for a single-household install |
| Prohibited | Bypassing measures to download content; "illegal or unauthorized use, streaming, download, or sharing of music" | MixSync never fetches Deezer audio; metadata suggestions only |

Sources: [Deezer terms of use](https://developers.deezer.com/termsofuse) · [Guidelines](https://developers.deezer.com/guidelines)

## Soulseek
| Rule | What the official rules say | What MixSync does |
|---|---|---|
| Clients | At most **5 clients from the same IP** | MixSync uses one slskd client; the docs warn if other clients run on the same network |
| Bots | Bots and scripts that don't implement the full set of Soulseek features aren't allowed | MixSync never speaks the protocol itself; it drives **slskd, a full client** |
| Content | Soulseek doesn't condone sharing copyrighted material; share only what you're allowed to | It's the user's responsibility; MixSync shares only the folders configured in slskd |

These are community norms, not written rules, and MixSync follows them:
- **Share back.** The library is shared through slskd. Users who download without sharing get banned by peers.
- Cap concurrent searches (default 2) and debounce duplicates. Failed searches back off: 1 h → 6 h → 24 h → weekly.
- Don't queue many files from a single peer at once.

Source: [Soulseek rules](https://www.slsknet.org/news/node/681)

## Caching
- Persistent server-side response cache in `/config` for MB, CAA, AcoustID, ListenBrainz, and Last.fm.
- MB entities 7 days, searches 1 day. Last.fm follows its HTTP headers and stays under 100 MB.
- An optional **local MusicBrainz mirror** (musicbrainz-docker) can replace the public API, which is useful for large migrations.

## Jittered background jobs
Watchlist checks, unverified re-checks, and discovery refreshes are spread randomly across their whole interval and never pinned to a clock time, so MixSync's jobs and other installs don't spike together.
