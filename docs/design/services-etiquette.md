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
| Spotify | unpublished quotas, rolling 30 s window | 1 req/s, never cached, `Retry-After` honored | one-shot playlist import on request (`MIXSYNC_SPOTIFY_CLIENT_ID`), library match only, never feeds downloads ([Spotify](#spotify)) |
| Discogs | 60/min authed, 25/min not | 50/min | release search and tracklist, on demand, per-user token ([Mark unverified](unverified-and-mb-submit.md)) |
| Deezer | none published | 1 req/s | album search and tracklist, on demand ([Mark unverified](unverified-and-mb-submit.md)) |
| Soulseek | 5 clients per IP | 1 slskd client | core |
| Prowlarr | per indexer, enforced by Prowlarr | 1 req / 2 s (searches and `.torrent`/`.nzb` fetches, including the re-fetch at enqueue and the one redirect to the indexer they may take) | torrents with qBittorrent, NZBs with SABnzbd ([sources](../../src/mixsync/sources/README.md#prowlarr)) |
| qBittorrent | local service | unlimited, like slskd | torrent downloads; seeding is left to its ratio rules ([sources](../../src/mixsync/sources/README.md#qbittorrent)) |
| SABnzbd | local service | unlimited, like slskd | NZB downloads ([sources](../../src/mixsync/sources/README.md#sabnzbd)) |

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

## Spotify
**ToS check for #337 (rechecked 2026-11-10 against the live Developer Policy and Developer Terms v10, effective 15 May 2025). Verdict: not permitted, because the only user-facing route, queuing P2P downloads from Spotify playlists, falls under the Terms' "illegal or unauthorized purpose" ban.** Owning the music doesn't change this: the clauses below cover what the app facilitates and where Spotify Content goes, and most jurisdictions don't treat downloading (or seeding) another copy as covered by owning one. #337 was rescoped on 2026-11-10 to the library-match-only import under [Rescoped design](#rescoped-design-337).

The analysis below is of the original live sync.

What mixsync would do: read-only `playlist-read-private`, a bring-your-own client ID (development mode: "Up to 5 authenticated Spotify users", and "The app owner must have a Spotify Premium account"; extended quota is for organizations with "at least 250k MAUs"), OAuth per user, self-imposed 1 req/s, and `Retry-After` honored ("Consider waiting for the number of seconds specified in `Retry-After`"; limit is a rolling 30 s window). Flow: Spotify playlist, resolve to MusicBrainz, missing tracks become `Request(source=playlist)` after owner confirmation, downloaded via Soulseek/torrent/Usenet.

Against:
- Terms IV.2.3: "Do not use the Spotify Platform, Spotify Service or Spotify Content for any illegal or unauthorized purpose". Policy I.3: "Don't break the law or encourage or enable others to do so." Most P2P downloads of commercial music are unauthorised copies, so feeding Spotify playlists into them is the use this clause excludes. This is the same reason [Last.fm](#lastfm) is deferred.
- Terms IV.2.8.1 rules out promoting, with Spotify materials, an app that "facilitates, or condones" "the illegal or unauthorized use or sharing of audio and/or audiovisual content".
- Terms II.8 counts "metadata, playlists, and user data" as Spotify Content. Terms IV.2.5: "do not transfer Spotify Content to third parties", which sending track names to download sources does.
- Policy III.5: "Do not create any product or service which is integrated with streams or content from another service."
- Terms IV.2.2(c) bars "facilitating "stream ripping" or other functionalities that make it easier for users to capture" content. Unclear whether fetching a copy elsewhere counts, since no Spotify stream is captured.
- Terms IV.3.1: no "databases of Spotify Content" beyond what is "strictly necessary", and "Do not store Spotify Content indefinitely". A mirrored playlist kept in the DB needs a refresh-or-delete rule.

For:
- Policy III.9 allows transferring "the metadata of the user's playlists to another service" at the user's request. That covers a one-shot, user-triggered import of a playlist's track list. It does not obviously cover a live sync that queues downloads.
- Terms III.1.1 licenses apps "for private personal use", which a single-household, bring-your-own-client-ID install is.

### Rescoped design (#337)
- A manual one-shot import under Policy III.9: the user presses "Import". No live sync, no polling.
- Match against the local library only: build a Navidrome playlist from files already owned, and *list* missing tracks.
- Spotify-sourced data never becomes a `Request` and never reaches a download source. This removes the IV.2.3, IV.2.5 and IV.2.8.1 problems; keep it under test. Policy III.5 is still arguable; III.9 is the explicit carve-out for moving a user's playlist to another service, and this design does no more than that.
- Request only `playlist-read-private` (Policy I.1.1). Drop raw Spotify playlist data once the import finishes (Terms IV.3.1). Delete a user's Spotify data when they disconnect (Terms V.8; within 5 days per Appendix A 5.c).
- Self-imposed 1 req/s and honor `Retry-After`.

As shipped (`discovery/spotify_api.py`, `web/routes/spotify.py`):
- Authorization Code with PKCE, no client secret. The redirect URI is MixSync's own `/spotify/callback` at the address the user opened it on, shown on the account page; Spotify accepts only `https://` or `http://127.0.0.1:PORT`. The verifier and state ride in an encrypted cookie bound to the login session. The refresh token is the user's `spotify` link, Fernet-encrypted; access tokens are memory only. A refused refresh (`invalid_grant`, or the 6-month expiry) marks the link "Reconnect Spotify".
- Spotify is called only when a user opens the playlist picker (own and collaborative playlists only) or presses Import. An import stores just artist, title and ISRC per track as the mirror's entries (no raw responses). Scheduled and "Sync now" syncs re-match those stored entries and never call Spotify; importing again refreshes them.
- `request_missing` refuses `spotify_api` mirrors and the page shows no request button; tested in `tests/unit/acquire/test_spotify_api_mirror.py`.
- Disconnect deletes the link and the user's `spotify_api` mirrors at once; files and playlists already in Navidrome are left alone. Settings export leaves out Spotify links and imports.

The existing file-import path (#334, no Spotify API, no Spotify account) is not covered by these terms, which govern use of the Spotify Platform. [Developer Policy](https://developer.spotify.com/policy), [Developer Terms](https://developer.spotify.com/terms)

## Discogs and Deezer (fallbacks, off by default)
- Discogs (`MIXSYNC_DISCOGS_SUGGESTIONS`, default off): 60 req/min authenticated, 25 unauthenticated, per IP over 60 s; service `discogs`, bucket at 50/min; `X-Discogs-Ratelimit-Remaining: 0` blocks the bucket for 60 s (a moving window, no reset header); unique UA; each reviewer links their own personal token on the account page (no shared token, no unauthenticated access). Requests carry `Authorization`, so they are never cached. Only a reviewer pressing "Suggest" on Mark unverified causes requests (a search, then a release); never on page load, in VERIFY or in a job. Numbers come from an OpenAPI copy (the official page blocks fetching): re-check by hand before enabling.
- Deezer (`MIXSYNC_DEEZER_SUGGESTIONS`, default off): no quota; service `deezer`, 1 req/s, cached a day. Only a reviewer pressing "Suggest from Deezer" on Mark unverified causes requests (a search, then an album and its tracks); never on page load, in VERIFY or in a job. Terms limit use to private family scope, non-commercial, so metadata suggestions only for a single-household install; never fetch audio. [Terms](https://developers.deezer.com/termsofuse)

## Soulseek
- At most 5 clients per IP: MixSync uses one slskd client. Bots must implement the full protocol: MixSync drives slskd, a full client, and never speaks the protocol itself.
- Share back through slskd (users who don't share get banned by peers). Sharing copyrighted material is the user's responsibility.
- Cap searches in flight across all workers (`MIXSYNC_SLSKD_MAX_SEARCHES`, default 2; extra searches wait). A repeated query within `MIXSYNC_SLSKD_SEARCH_DEBOUNCE` (default 300 s) reuses the result. A request whose search finds nothing backs off 1 h → 4 h → 16 h → 64 h → weekly.
- Don't queue many files from one peer at once.
- Cap concurrent downloads (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`, default 3, counting active transfers, not albums). A full cap defers a `download` job without spending an attempt. Slow transfers (`MIXSYNC_SLOW_DOWNLOAD_*`, see [deploy](../../deploy/README.md#environment-variables)) stop counting so a stalled peer cannot block the queue.

[Soulseek rules](https://www.slsknet.org/news/node/681)
