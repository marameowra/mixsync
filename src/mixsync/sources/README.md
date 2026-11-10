# `sources`

## Purpose
Adapters that find and download music: slskd (Soulseek), and Prowlarr search (torrent and Usenet indexers) with torrents downloaded by qBittorrent and NZBs by SABnzbd. yt-dlp is out.

## Owns / does not own
- **Owns:** `SlskdSource` (a `DownloadSource` over the slskd REST API), `ProwlarrSource` (search over Prowlarr's API, downloads through `Qbittorrent`, the qBittorrent Web API client, and `Sabnzbd`, the SABnzbd API client), the response mapping to `core` types, and the search query builder (`query.py`).
- **Does not own:** choosing a candidate (`match`), moving files into the library (`library`), or the Soulseek protocol (slskd does that).

## Rules / invariants
- Downloads land in `/downloads` (slskd's folder). This package never writes to `/data`.
- Talk to slskd through `ratelimit.PoliteClient("slskd", ...)`. `slskd-api` is a sync, untyped wrapper, so `slskd.py` is a thin httpx client.
- At most 3 concurrent downloads (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`), counting only `InProgress` file transfers; slow ones stop counting ([etiquette](../../../docs/design/services-etiquette.md#soulseek)). `active_downloads()` feeds `acquire.gate.DownloadGate`.
- Never queue more than one album folder per peer at a time.
- `TransferInfo.local_path` is always `None`: slskd's API does not report it.
- `search()` holds one of `MIXSYNC_SLSKD_MAX_SEARCHES` DB-leased slots (`ratelimit.Slots`) while it runs, so the cap holds across workers; extra searches wait. The same query (trimmed, case-folded) in flight or within `MIXSYNC_SLSKD_SEARCH_DEBOUNCE` shares one slskd search (per worker process). The no-results backoff lives in `acquire`.

## Prowlarr
- The worker registers it (as `prowlarr`) only when Prowlarr and at least one download client are configured: qBittorrent (URL and dir) for `torrent`, SABnzbd (URL, API key and dir) for `usenet`. `protocols` holds only those (`sources.prowlarr_protocols`); other releases are skipped before any fetch, so no request picks a candidate nothing can download. Without a client, `enqueue`/`status` raise `PermanentError` and the pipeline moves to the next candidate. A candidate goes to the client its id names: `infohash` to qBittorrent, `nzb_hash` to SABnzbd.
- `GET /api/v1/search?query=&type=search&categories=3000` with `X-Api-Key`, through `PoliteClient("prowlarr", ...)` at 1 request / 2 s; every `.torrent`/`.nzb` fetch uses the same bucket. Prowlarr keeps its own per-indexer limits; mixsync never overrides them.
- At most the first 2 query variants (`max_queries`); the search runs inside a `search` job, so it counts against `max_searches` like a Soulseek search.
- Fields read (verified against Prowlarr source, develop @ 61476876ec3cb4526c21782f468a205ce608e7a9 (ReleaseResource.cs, SearchController.cs:198-199, NewznabController.cs:265-314, STJson WhenWritingNull)): `title`, `size` (bytes; 0 when unknown, never null), `indexer` (name), `protocol` (`torrent`/`usenet`), `downloadUrl` (always Prowlarr's proxy link to the `.torrent`/`.nzb`; absent when unset), `seeders`. Null fields are omitted from the JSON, not sent as `null`: Usenet results have no `seeders`, `leechers`, `magnetUrl` or `infoHash`. `magnetUrl` is also a Prowlarr proxy link, not a raw magnet. Unused: `guid` (indexer-specific, may hold a passkey), `indexerId`, `infoUrl`, `magnetUrl`.
- The proxy link answers a Usenet release with a `301` to the indexer's own NZB URL, always, and a torrent one the same way when the indexer's Redirect option is on (Torznab, HDBits, FileList, Orpheus, Nebulance, Redacted), or with a `301` to a `magnet:` URI. MixSync follows one redirect to an `http(s)` Location, without Prowlarr's `X-Api-Key` header; that URL carries the indexer's key, so it is never stored or logged. A magnet or other scheme, or a second redirect, skips the release (or fails the enqueue). The proxy's own errors are XML, not JSON: `410` unavailable, `429` with `Retry-After`, `500`.
- A candidate stores `url` (the `downloadUrl` with its `apikey` param stripped) and `infohash` (the v1 SHA-1 of the `.torrent`'s `info` dict) or `nzb_hash` (the SHA-1 of the NZB's file list, names and sizes, so an indexer re-serving the release with other headers still matches). `enqueue` fetches the file again the same way, with the key added back (never stored; httpx's request log is held at WARNING so it is never logged), refuses it if the hash changed, and hands its bytes to qBittorrent or SABnzbd, so neither key lands in a client.
- Ranking before any fetch: title similarity to the query, then a plausible album size (20 MB to 3 GB), then seeders. Results with no `downloadUrl` (magnet-only) are skipped. Only the top 3 have their file list fetched; a magnet or second redirect, an unreadable file, or a link off the Prowlarr host (the API key would leak) skips the release.
- A candidate is one release: `username` is the indexer, `folder` the release title, `files` its audio files (paths joined with `\` like slskd's), `seeders` set for torrents. A release whose audio sits in more than one album folder (disc folders fold in) is a pack or discography and is dropped (#328 will pick files instead). An `.nzb` of RAR parts has no audio files and scores as empty.
- `match.candidates.rank()` scores it like any folder. Soulseek tie-breakers (free slot, queue, speed) and the peer-health penalty apply to `slskd` candidates only; indexer releases break ties on seeders, and one indexer may offer many releases (Soulseek keeps one folder per peer).

## qBittorrent
- Web API v2 through `PoliteClient("qbittorrent", ...)` (local, unlimited, like slskd). Logs in (`auth/login`) on the first 403; the `SID` cookie stays in the client. Fields read per the official WebUI API docs (no live instance was checked): `hash`, `progress`, `state`, `completed`, `amount_left`.
- `torrents/add` under category `mixsync` with `savepath` = `MIXSYNC_QBITTORRENT_DIR` (`autoTMM=false`, `contentLayout=Original`), so a file lands at `<dir>/<torrent name>/<path>`, the candidate's path. The dir is the same path in both containers; torrent paths that could leave it are refused.
- Done = `progress` 1. `error`/`missingFiles`, or a torrent gone from the `mixsync` category, is a failed transfer. Lookups filter on the category: a user's own torrent is never touched.
- Seeding downloads ([ADR 0022](../../../docs/decisions/0022-seeding-downloads-copied-never-released.md)): `clear` deletes a torrent with its files only while it is in a downloading state with `amount_left` > 0 (never by `progress` alone: a recheck restarts it from 0), and stops it first, then checks again, so it can't finish in between. A finished torrent is never removed, and the pipeline never calls `clear` after importing one.

## SABnzbd
- API through `PoliteClient("sabnzbd", ...)` (local, unlimited, like slskd). Every call is a POST with `apikey` in the form body, never the URL, so no log shows it. Fields read per SABnzbd's official API docs (no live instance was checked): queue `nzo_id`, `filename`, `cat`, `mb`, `mbleft`; history `nzo_id`, `name`, `category`, `status`, `storage`, `bytes`.
- `mode=addfile` with the NZB's bytes under category `mixsync` and `nzbname` `mixsync-<nzb_hash>`, so a re-run finds the job by name without storing its `nzo_id` (and adds nothing if it is already there). Two requests that pick the same release share that one job, and a leftover `Failed` history entry under the name makes a re-add fail at once. Point the category's folder at `MIXSYNC_SABNZBD_DIR`, the same path in both containers; a job whose `storage` is not strictly inside it is refused.
- Status reads the queue, then history (in that order, so a job moving between them is not missed), both filtered by `cat=mixsync` and the job name and checked again in code: a user's own job is never read or touched. Done = history `Completed`; candidate files are matched by name under `storage`. `Failed` (aborted jobs too), or a job gone from both, is a failed transfer; a post-processing step is in progress.
- Usenet downloads do not seed: after a clean import the pipeline releases them (hash-verified copy, then journaled delete; [data safety](../../../docs/design/data-safety.md)), then `clear` deletes the history entry (`archive=0`). `clear` passes `del_files=1` only for a queued or failed job, whose files are partial; SABnzbd never deletes a completed job's folder.

## May import
`core`, `ratelimit`.

## Design doc
[Architecture](../../../docs/architecture.md#components-and-delegation) · [Respecting services: Soulseek](../../../docs/design/services-etiquette.md#soulseek)
