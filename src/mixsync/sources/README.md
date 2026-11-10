# `sources`

## Purpose
Adapters that find and download music: slskd (Soulseek), and Prowlarr search (torrent and Usenet indexers) with torrents downloaded by qBittorrent (SABnzbd for Usenet is #327). yt-dlp is out.

## Owns / does not own
- **Owns:** `SlskdSource` (a `DownloadSource` over the slskd REST API), `ProwlarrSource` (search over Prowlarr's API, downloads through `Qbittorrent`, the qBittorrent Web API client), the response mapping to `core` types, and the search query builder (`query.py`).
- **Does not own:** choosing a candidate (`match`), moving files into the library (`library`), or the Soulseek protocol (slskd does that).

## Rules / invariants
- Downloads land in `/downloads` (slskd's folder). This package never writes to `/data`.
- Talk to slskd through `ratelimit.PoliteClient("slskd", ...)`. `slskd-api` is a sync, untyped wrapper, so `slskd.py` is a thin httpx client.
- At most 3 concurrent downloads (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`), counting only `InProgress` file transfers; slow ones stop counting ([etiquette](../../../docs/design/services-etiquette.md#soulseek)). `active_downloads()` feeds `acquire.gate.DownloadGate`.
- Never queue more than one album folder per peer at a time.
- `TransferInfo.local_path` is always `None`: slskd's API does not report it.
- `search()` holds one of `MIXSYNC_SLSKD_MAX_SEARCHES` DB-leased slots (`ratelimit.Slots`) while it runs, so the cap holds across workers; extra searches wait. The same query (trimmed, case-folded) in flight or within `MIXSYNC_SLSKD_SEARCH_DEBOUNCE` shares one slskd search (per worker process). The no-results backoff lives in `acquire`.

## Prowlarr
- The worker registers it (as `prowlarr`) only when Prowlarr and qBittorrent (URL and dir) are both configured, with `protocols={"torrent"}`: Usenet releases are skipped before any fetch until SABnzbd (#327), so no request picks a candidate nothing can download. Without a client, `enqueue`/`status` raise `PermanentError` and the pipeline moves to the next candidate.
- `GET /api/v1/search?query=&type=search&categories=3000` with `X-Api-Key`, through `PoliteClient("prowlarr", ...)` at 1 request / 2 s; every `.torrent`/`.nzb` fetch uses the same bucket. Prowlarr keeps its own per-indexer limits; mixsync never overrides them.
- At most the first 2 query variants (`max_queries`); the search runs inside a `search` job, so it counts against `max_searches` like a Soulseek search.
- Fields read (from Prowlarr's OpenAPI `ReleaseResource`; no live instance was checked): `title`, `size` (bytes), `indexer` (name), `protocol` (`torrent`/`usenet`), `downloadUrl` (Prowlarr's proxy link to the `.torrent`/`.nzb`, null for magnet-only results, and may redirect to a magnet), `magnetUrl`, `seeders` (null for Usenet). Unused: `guid` (indexer-specific, may hold a passkey), `indexerId`, `infoUrl`.
- A candidate stores `url` (the `downloadUrl` with its `apikey` param stripped) and `infohash` (the v1 SHA-1 of the `.torrent`'s `info` dict). `enqueue` fetches the `.torrent` again with the key added back (never stored; httpx's request log is held at WARNING so it is never logged), refuses it if the info hash changed, and adds it to qBittorrent.
- Ranking before any fetch: title similarity to the query, then a plausible album size (20 MB to 3 GB), then seeders. Results with no `downloadUrl` (magnet-only) are skipped. Only the top 3 have their file list fetched; a 3xx (magnet redirect), an unreadable file, or a link off the Prowlarr host (the API key would leak) skips the release.
- A candidate is one release: `username` is the indexer, `folder` the release title, `files` its audio files (paths joined with `\` like slskd's), `seeders` set for torrents. A release whose audio sits in more than one album folder (disc folders fold in) is a pack or discography and is dropped (#328 will pick files instead). An `.nzb` of RAR parts has no audio files and scores as empty.
- `match.candidates.rank()` scores it like any folder. Soulseek tie-breakers (free slot, queue, speed) and the peer-health penalty apply to `slskd` candidates only; indexer releases break ties on seeders, and one indexer may offer many releases (Soulseek keeps one folder per peer).

## qBittorrent
- Web API v2 through `PoliteClient("qbittorrent", ...)` (local, unlimited, like slskd). Logs in (`auth/login`) on the first 403; the `SID` cookie stays in the client. Fields read per the official WebUI API docs (no live instance was checked): `hash`, `progress`, `state`, `completed`.
- `torrents/add` under category `mixsync` with `savepath` = `MIXSYNC_QBITTORRENT_DIR` (`autoTMM=false`, `contentLayout=Original`), so a file lands at `<dir>/<torrent name>/<path>`, the candidate's path. The dir is the same path in both containers; torrent paths that could leave it are refused.
- Done = `progress` 1. `error`/`missingFiles`, or a torrent gone from the `mixsync` category, is a failed transfer. Lookups filter on the category: a user's own torrent is never touched.
- Seeding downloads ([ADR 0022](../../../docs/decisions/0022-seeding-downloads-copied-never-released.md)): `clear` deletes a torrent with its files only while `progress` < 1 (dropped before it finished); a finished torrent is never removed.

## May import
`core`, `ratelimit`.

## Design doc
[Architecture](../../../docs/architecture.md#components-and-delegation) · [Respecting services: Soulseek](../../../docs/design/services-etiquette.md#soulseek)
