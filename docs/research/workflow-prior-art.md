# Prior art: the request → acquire → verify → organize → publish workflow

Research date: 2026-10-17. Scope: how Lidarr, beets, soularr, slskd, Picard and SoulSync handle each stage of
mixsync's pipeline ([architecture](../architecture.md#pipeline)), and which failure modes their code and issue
trackers show. Primary sources only. Code links are pinned to a commit:

| Tool | Pinned at |
|---|---|
| Lidarr | [`aec8802`](https://github.com/Lidarr/Lidarr/tree/aec88028d036707619baacbbb840b30c2d716318) (`develop`) |
| beets | [`18967aa`](https://github.com/beetbox/beets/tree/18967aad9d36ca29fe2a370c3eca9beae3b46749) (`master`) |
| soularr | [`2037399`](https://github.com/mrusse/soularr/tree/2037399aa57433f9032bbae09833439830d2b409) (`main`) |
| slskd | [`2242128`](https://github.com/slskd/slskd/tree/22421286fe2216950a01fa0fb7f2a82836ff9074) (`master`); Soulseek.NET [`c07c187`](https://github.com/jpdillingham/Soulseek.NET/tree/c07c187f0eee16fa4dde548120ad733f7ebe3f44) |
| Picard | [`84862c5`](https://github.com/metabrainz/picard/tree/84862c534c7a801b726035ee445b0903f3d3fe4a) (`master`) |
| SoulSync | [`9e35006`](https://github.com/Nezreka/SoulSync/tree/9e35006055a5161cbe26f3b7af010ab5f5f2b093) (`main`). Only the README was read, so feature claims are the project's own description and the implementation is **unverified**. |

The two extra tools: **Picard** shows how MusicBrainz's own tagger matches; **SoulSync** is the tool mixsync
replaces ([v1 scope](../README.md#status)). Headphones was left out.

---

## 1. Summary

1. **If the pre-download filter and the import gate disagree, the same album downloads forever.** soularr picks a folder
   by filename ratio, but Lidarr's import gate is fixed at 80% and can't be configured, so failed albums pile up in
   `failed_imports` and get downloaded again on every run until the disk fills
   ([soularr#68](https://github.com/mrusse/soularr/issues/68), [#137](https://github.com/mrusse/soularr/issues/137), [#164](https://github.com/mrusse/soularr/issues/164)). The fix was a per-album denylist ([soularr L1226](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L1226)). mixsync already shares one scorer across both stages and keeps a `tried` list. What it lacks is a **cap on attempts or disk use per request**.
2. **Add a margin rule.** beets will not auto-apply when the best two candidates are close, and it caps the confidence
   when certain penalties fire (`rec_gap_thresh: 0.25`, `max_rec: {missing_tracks: medium}`,
   [config_default.yaml L172-178](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/config_default.yaml#L172-L178)).
   Picard has a "minimum margin between best and second-best match" option ([docs](https://picard-docs.musicbrainz.org/en/latest/config/options_matching.html)). mixsync's bands use the absolute distance only ([matching](../design/matching.md#scoring-and-bands)).
3. **mixsync's mandatory AcoustID check is stricter than any prior art. Keep it.** Lidarr fingerprints only when the tag
   match is weak ([IdentificationService L155-170](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/IdentificationService.cs#L155-L170)). beets drops AcoustID errors and scores below 0.5 without telling the user ([chroma.py L120-139](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py#L120-L139)).
4. **Never remove the old file before the new one is in place, and never rewrite library files in place.** Lidarr's
   upgrade path deletes the existing track before it moves the new one in. With no recycle bin configured, the delete is permanent
   ([UpgradeMediaFileService L57-85](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/UpgradeMediaFileService.cs#L57-L85), [RecycleBinProvider L43-48](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/RecycleBinProvider.cs#L43-L48), user report [Lidarr#5846](https://github.com/Lidarr/Lidarr/issues/5846)). SoulSync corrupted 1,249 library FLACs by retagging them in place, which its maintainer confirmed ([SoulSync#1000](https://github.com/Nezreka/SoulSync/issues/1000)). Both cases back [data-safety](../design/data-safety.md) rules 1–3.
5. **slskd searches can hang.** Searches that never complete and can't be cancelled are a long-running report ([slskd#1306](https://github.com/slskd/slskd/issues/1306), [soularr#41](https://github.com/mrusse/soularr/issues/41)). Clients need a hard deadline and an explicit cancel or delete. slskd's defaults also cap a search at **100 responses** ([SearchRequest.cs L80-83](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Search/API/DTO/SearchRequest.cs#L80-L83)). mixsync sends only `searchText`, so it gets all the defaults.
6. **slskd can delete your staging files.** Its `retention.files.complete` setting deletes downloaded files by last-access age ([config.md "Data Retention"](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/docs/config.md#data-retention)). mixsync leaves rejected and review candidates in `/downloads` ([requests.py L108-109](../../src/mixsync/acquire/requests.py)). The required slskd retention settings should be documented, or the files should live somewhere mixsync owns.
7. **Retry single files before dropping a peer.** soularr requeues an errored file up to 4 times. It treats `Rejected`
   as a soft limit and waits for the album's other files to settle first ([soularr L877-940](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L877-L940)). mixsync maps any `Completed` state other than `Succeeded` to a failed candidate ([slskd.py `_state`](../../src/mixsync/sources/slskd.py)).
8. **Requesting a specific edition is fragile, so request the release group.** soularr's chosen release (a 16-track
   edition) wasn't on Soulseek while the 8-track one was ([soularr#11](https://github.com/mrusse/soularr/issues/11)). Lidarr then rejects other editions at import ([#164](https://github.com/mrusse/soularr/issues/164)). This supports mixsync's release-group request and its canonical-release policy ([library](../design/library.md#release-group-canonicalization)).
9. **Rescans should be batched and scoped.** Lidarr queues Plex updates per artist and scans only the artist's path ([PlexServer.cs L78-93](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServer.cs#L78-L93), [PlexServerService.cs L136-146](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServerService.cs#L136-L146)). Navidrome has a file watcher on by default (`Scanner.WatcherWait` 5s, [docs](https://www.navidrome.org/docs/usage/configuration/options/)), so an explicit rescan is a backstop. mixsync already coalesces rescans ([requests.py `enqueue_rescan`](../../src/mixsync/acquire/requests.py)).
10. **Detecting fake lossless files and padded previews is now expected.** SoulSync advertises a fake-lossless detector and ffmpeg checks for padded previews and silence ([README L137](https://github.com/Nezreka/SoulSync/blob/9e35006055a5161cbe26f3b7af010ab5f5f2b093/README.md?plain=1#L137); implementation unverified). mixsync checks only the size-implied bitrate before download ([candidates.py `FLAC_MIN_KBPS`](../../src/mixsync/match/candidates.py)).

---

## 2. Stage by stage

### 2.1 Request modeling

| Tool | Unit of request | Where requests come from | Notes |
|---|---|---|---|
| Lidarr | Monitored **artist**; albums monitored via `MonitorTypes` (All/Future/Missing/Existing/Latest/First/None) and `NewItemMonitorTypes` (All/None/New) | Artist add, import lists, `MissingAlbumSearch`, `CutoffUnmetAlbumSearch` | [MonitorTypes.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Music/Model/MonitorTypes.cs), [NewItemMonitorTypes.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Music/Model/NewItemMonitorTypes.cs), [AlbumSearchService L16-18](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/IndexerSearch/AlbumSearchService.cs#L16-L18) |
| soularr | Lidarr's **wanted** list (`missing` or `cutoff_unmet`) | Paged reads of Lidarr's wanted list: `all`, `first_page` or `incrementing_page` | [get_records L1109-1162](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L1109-L1162); release picked by country/format/"most common track count" ([choose_release L211](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L211)) |
| beets | A **directory** (album) or file (singleton) on disk; no wishlist | `beet import <path>` | Tracks finished directories only with `incremental` ([config.rst L699](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/docs/reference/config.rst#L699)) |
| SoulSync | Wishlist item, plus watchlist follow (artist/label) and requests with admin approval | Playlists, watchlist, catalog search, `POST /api/v1/request` | README L167-171, L322 (self-described) |

Lessons:
- Lidarr depends on its own metadata server. When that server broke after an MB schema change, searches failed for
  everyone ([Lidarr#5498](https://github.com/Lidarr/Lidarr/issues/5498), contributor comment). This supports
  [ADR 0001](../decisions/0001-orchestrator-not-monolith.md)'s choice to go straight to MB.
- soularr's `incrementing_page` mode exists so a large wanted list isn't searched all at once. It also enforces
  `minimum_search_interval` between searches ([L766](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L766)).
- When ownership is matched by name, it fails on diacritics and "(live)" suffixes ([SoulSync#101](https://github.com/Nezreka/SoulSync/issues/101)).
  This supports MBID-keyed ownership ([library](../design/library.md#identifiers-stored-per-track)).

### 2.2 Search and pre-download ranking

| Tool | Query | Filter | Ranking |
|---|---|---|---|
| Lidarr (torrent/Usenet) | Indexer search per album | Decision specs grouped by priority; each yields a rejection that is Permanent or Temporary ([DownloadDecisionMaker L210](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/DecisionEngine/DownloadDecisionMaker.cs#L210)) | **Lexicographic** comparator: quality → custom format score → year match → protocol → indexer priority → torrent peers → album count → Usenet age → size ([DownloadDecisionComparer L37-48](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/DecisionEngine/DownloadDecisionComparer.cs#L37-L48)) |
| soularr (slskd) | Album title, optionally with the artist; words on `search_blacklist` stripped ([L434-455](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L434-L455)) | slskd `filterResponses`, `maximumPeerQueueLength` (50), `minimumPeerUploadSpeed` ([L457-465](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L457-L465)); file count must equal the track count ([L383-386](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L383-L386)) | **First match wins.** Filetypes are tried in preference order; within one, the first user whose folder has every track at `difflib` ratio > 0.8 is used ([album_match L91-133](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L91-L133), [try_enqueue L589](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L589)) |
| slskd (API) | `POST /api/v0/searches` with `searchText` | `fileLimit` 10,000, `responseLimit` 100, `filterResponses` true, `maximumPeerQueueLength` 1,000,000, `minimumPeerUploadSpeed` 0, `minimumResponseFileCount` 1 (defaults) | None; results come back in arrival order ([SearchRequest.cs L51-95](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Search/API/DTO/SearchRequest.cs#L51-L95)) |
| SoulSync | Several sources in a user-ordered "hybrid chain"; falls through to the next when the quality profile isn't met | Quality profile ladder with cutoff | README L116, L136 (self-described) |

Notes:
- **`searchTimeout` units.** The slskd DTO comment says seconds (default 15), and that the timeout counts "from the time of
  the last response" ([L91-95](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Search/API/DTO/SearchRequest.cs#L91-L95)).
  The value is passed through unchanged to Soulseek.NET, which documents **milliseconds**, default 15000
  ([SearchOptions.cs L36, L53](https://github.com/jpdillingham/Soulseek.NET/blob/c07c187f0eee16fa4dde548120ad733f7ebe3f44/src/Options/SearchOptions.cs#L36-L53)).
  soularr sends 5000 as milliseconds. Treat the value as ms.
- **Stuck searches.** In [slskd#1306](https://github.com/slskd/slskd/issues/1306) the maintainer pointed to fixes in 0.22.2 and #1310, but users still report
  needing daily restarts. The suspected cause is SignalR keeping DB updates from committing. soularr's workaround is a client-side deadline of
  `search_timeout + 60s` and then giving up ([L474-479](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L474-L479)).
  It also has an optional `delete_searches` setting.
- **Folder caching.** soularr caches each peer's directory listing per run and marks peers whose browse fails as "broken" for that run ([check_for_match L348-391](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L348-L391)).
- **Lidarr's "Temporary" vs "Permanent" rejections** keep "not yet" (for example a delay profile) separate from "never". mixsync's stage-1 `REJECT` band has no such split.

### 2.3 Download tracking and retry

| Tool | How completion is detected | Per-file failure | Whole-download failure | Next candidate |
|---|---|---|---|---|
| Lidarr | Polls the download client; `TrackedDownload` states `Downloading → ImportPending → Importing → Imported / ImportFailed / ImportBlocked` ([CompletedDownloadService L59-200](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/CompletedDownloadService.cs#L59-L200)) | n/a (the client handles it) | `DownloadFailedEvent` → blocklist ([BlocklistService L23-26](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Blocklisting/BlocklistService.cs#L23-L26)) | `AutoRedownloadFailed` searches again, except after an interactive grab unless that is enabled too ([RedownloadFailedDownloadService L33-45](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/RedownloadFailedDownloadService.cs#L33-L45)) |
| soularr | Polls slskd transfers every 5s | Errored/Cancelled/TimedOut/Aborted: requeue the file, at most 4 times. `Rejected`: wait for the other files to settle, then requeue, capped at 1.2 × file count ([L877-940](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L877-L940)) | `stalled_timeout` (3600s) or all files remotely queued past `remote_queue_timeout` → cancel and `rmtree` the folder ([L177-187](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L177-L187), [L953-958](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L953-L958)) | Only on the next scheduled run (re-search) |
| slskd | Transfer state flags: `Requested, Queued, Initializing, InProgress, Completed` + one of `Succeeded, Cancelled, TimedOut, Errored, Rejected, Aborted`; `Locally/Remotely` for queued ([TransferStates.cs](https://github.com/jpdillingham/Soulseek.NET/blob/c07c187f0eee16fa4dde548120ad733f7ebe3f44/src/TransferStates.cs)). Events `DownloadFileComplete`, `DownloadDirectoryComplete` ([Events.cs L79-95](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Events/Types/Events.cs#L79-L95)) | — | — | — |
| SoulSync | — | — | — | Wishlist "retried automatically with progressive backoff"; diagnostics show why each release was refused (README L170, L242; self-described) |

Failure modes from issues:
- **Eager whole-album removal.** soularr removes the whole album when one file errors. The maintainer defends this as "set
  and forget", while a contributor reports 10 copies of one album in `failed_imports` ([soularr#60](https://github.com/mrusse/soularr/issues/60)). The per-file requeue came later (code above).
- **Peer timeouts.** "The wait timed out after 5000 milliseconds" usually means the peer connection couldn't be made, according to the maintainer.
  Users traced it to a VPN ([slskd#1144](https://github.com/slskd/slskd/issues/1144)). This is a peer or network fault, not a file fault.
- **Stuck transfers.** Downloads stuck at 92–96% that couldn't be cancelled were blamed on out-of-order DB updates ([slskd#598](https://github.com/slskd/slskd/issues/598)).
  A byte-progress stall timer, like mixsync's `DOWNLOAD_STALL`, is the right defence.
- **Webhooks.** slskd webhooks and scripts are **at-least-once and may arrive out of order** (dedupe by event `Id`). A webhook is tried once by default with a 5s timeout.
  Script data comes from the network and can carry shell injection ([config.md Integrations](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/docs/config.md#integrations)).
- **Directory layout.** If the incomplete directory sits inside the downloads directory, soularr can move slskd's `incomplete` folder away ([soularr#45](https://github.com/mrusse/soularr/issues/45)).
  A contributor also reports that a shared directory gives timestamped filenames that hurt identification ([#164](https://github.com/mrusse/soularr/issues/164)).

### 2.4 Matching and verification (accept / reject / ask)

| Tool | Evidence | Score | Accept | Ask a human | Reject |
|---|---|---|---|---|---|
| beets | Tags; optional AcoustID via `chroma` | Weighted distance; weights in [config_default.yaml L172-210](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/config_default.yaml#L172-L210) (e.g. `album_id` 5, `track_id` 5, `artist`/`album`/`track_title` 3) | `strong` (< 0.04) auto-applies unless `timid` ([ui session L501-502](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/ui/commands/import_/session.py#L501-L502)) | `medium` ≤ 0.25; `low` = single candidate or gap ≥ 0.25; `none` = no conclusion → `none_rec_action: ask` ([match.py L269-320](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/autotag/match.py#L269-L320)) | In `quiet` mode anything but `strong` → `quiet_fallback: skip` ([session L374-379](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/ui/commands/import_/session.py#L374-L379)) |
| Lidarr | Tags, then AcoustID only if the match is poor | Same penalty names as beets; track length with a 10s grace over 30s ([DistanceCalculator L40-48](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/DistanceCalculator.cs#L40-L48)). Inferred to derive from beets; **provenance unverified** | New download: album distance ≤ 0.20 **and** worst track ≤ 0.40, **hard-coded constants** ([CloseAlbumMatchSpecification L12-13](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Specifications/CloseAlbumMatchSpecification.cs#L12-L13)) | `ImportBlocked` / manual "Interactive Import" (e.g. artist-name mismatch, [CompletedDownloadService L96-97](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/CompletedDownloadService.cs#L96-L97)) | `ImportFailed` with a reason string such as "Album match is not close enough: 64.9 % vs 80 %" |
| Picard | Tags + AcoustID | Weighted similarity: title 22, album 12, length 8, artist 6, tracknumber 6, totaltracks 5, discnumber 5, totaldiscs 4 ([metadata.py L67-75, L127-157](https://github.com/metabrainz/picard/blob/84862c534c7a801b726035ee445b0903f3d3fe4a/picard/metadata.py#L67-L157)) | Desktop tool: the user reviews and saves | Thresholds: min similarity, **min margin best vs second-best**, file-to-track similarity (default 0.4, [options.py L643-650](https://github.com/metabrainz/picard/blob/84862c534c7a801b726035ee445b0903f3d3fe4a/picard/options.py#L643-L650); [docs](https://picard-docs.musicbrainz.org/en/latest/config/options_matching.html)) | Below threshold → file stays unmatched |
| soularr | Filename ratio only | `difflib` ratio > 0.8 per track | Hands off to Lidarr `DownloadedAlbumsScan` ([L846-849](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L846-L849)) | None | Lidarr's message contains "Failed" → move to `failed_imports/`, add to denylist ([L861-871](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L861-L871)) |
| SoulSync | Exact IDs (ISRC, links in tags) → tags → filenames → AcoustID; ffmpeg checks, fake-lossless | "Confidence line" | Auto-import | Review queue | Quarantine with reason and uploader (README L137-138, L178-180; self-described) |

AcoustID handling compared:

| | When | On lookup error | Score floor | Effect |
|---|---|---|---|---|
| beets `chroma` | Every item when the plugin is on; `fpcalc` forced ([L105-107](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py#L105-L107)) | Log at debug and return: the file is matched on tags alone ([L120-130](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py#L120-L130)) | Top result only, `< 0.5` dropped ([L44, L136-139](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py#L136-L139)) | Adds candidates from releases shared by > 60% of tracks (max 5), and a `track_id` penalty when the recording isn't among the hits ([L231-270](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py#L231-L270)) |
| Lidarr | When there are no candidates, or album distance > 0.15, or worst track > 0.40, or extra/missing tracks ([L155-170](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/IdentificationService.cs#L155-L170)); `AllowFingerprinting` = Never/NewFiles/All | **unverified** | Lookup threshold 0.5 ([L209](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/IdentificationService.cs#L209)) | `recording_id` penalty when the AcoustID results don't contain the track ([DistanceCalculator L76](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/DistanceCalculator.cs#L76)) |
| mixsync | Every file | Retry, never import | — | Mandatory feature; a mismatch with a high score is a hard veto ([matching](../design/matching.md#stage-2-post-download-verification)) |

Failure modes:
- **Non-configurable import gate.** Lidarr's fixed 80% gate is the top complaint behind soularr's failed imports ([soularr#68](https://github.com/mrusse/soularr/issues/68), [#137](https://github.com/mrusse/soularr/issues/137)).
  The owner calls it "very tough to debug". Users can't see or tune the penalty that tipped the decision. mixsync's per-feature reasons and editable thresholds ([ADR 0005](../decisions/0005-explainable-matching-score.md)) address this directly.
- **Partial albums.** "Lidarr only imports full releases" (soularr owner, [#68](https://github.com/mrusse/soularr/issues/68)). For existing files Lidarr ignores missing and unmatched tracks, but not for new downloads ([CloseAlbumMatchSpecification L27-63](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Specifications/CloseAlbumMatchSpecification.cs#L27-L63)).
  mixsync allows a partial import when AcoustID confirms every present file ([match README](../../src/mixsync/match/README.md)).
- **Review UI churn.** Fixing a match in SoulSync's dialog led to wrong assignments because the layout shifted while async search results arrived ([SoulSync#186](https://github.com/Nezreka/SoulSync/issues/186)).

### 2.5 Organize: move, rename and tag safely

| Tool | Staging → library | Integrity check | Existing destination | Old or replaced file | Tag write |
|---|---|---|---|---|---|
| Lidarr | Move, or copy/hardlink when the client can't release files ([TrackFileMovingService L101-108](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackFileMovingService.cs#L101-L108)) | **Size only**, with rollback on mismatch ([DiskTransferService L475-513](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Common/Disk/DiskTransferService.cs#L475-L513)) | `overwrite` flag ([L396-410](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Common/Disk/DiskTransferService.cs#L396-L410)) | Upgrade deletes the old file **before** moving the new one; a recycle bin is optional and otherwise "deleting permanently" | **After** the move, in the library ([UpgradeMediaFileService L76-85](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/UpgradeMediaFileService.cs#L76-L85)) |
| beets | `copy: yes` by default, `move: no`; the docs warn "Moving files can be risky" ([config.rst L604-640](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/docs/reference/config.rst#L604-L640)) | None. Across filesystems: copy to a temp file in the destination dir, `os.replace`, then delete the source ([util L496-553](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/util/__init__.py#L496-L553)) | Checks `exists`, then `os.replace` (a check-then-act race) | `duplicate_action: ask` by default; options skip/keep/remove/merge/upgrade-by-bitrate ([config.rst L873-893](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/docs/reference/config.rst#L873-L893)) | `write: yes`; `resume: ask` after an interrupted import ([L688](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/docs/reference/config.rst#L688)) |
| soularr | `shutil.move` into a renamed "Artist - Album (Year)" folder, with best-effort rollback ([L776-815](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L776-L815)) | None | — | Failed imports go to `failed_imports/` with a numeric suffix ([L1029-1046](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L1029-L1046)) | Writes `albumartist`/`album` into the **downloaded** files before Lidarr imports them ([L819-829](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L819-L829)) |
| SoulSync | "Atomic album publishing", path templates, recycle bin with retention | — | — | Replaced only by "a measured upgrade" | README L136-141 (self-described). **Actual incident:** in-place album-tag rewrite of existing library files corrupted 1,249 FLACs ([#1000](https://github.com/Nezreka/SoulSync/issues/1000)) |

Lessons:
- None of the tools hash-verifies or fsyncs. Lidarr's size check is the strongest. mixsync's copy → fsync → hash → retag → no-replace rename ([data-safety §2](../design/data-safety.md#2-atomic-verified-imports), [fileops.py `renameat2(RENAME_NOREPLACE)`](../../src/mixsync/library/fileops.py)) is stronger than all of them.
- Both real data-loss cases above came from **changing library files outside a staged copy**: Lidarr deleted before moving, SoulSync wrote tags in place.

### 2.6 Publish: notify the media server

| Tool | Target | Mechanism |
|---|---|---|
| Lidarr | Plex | Rolling per-host queue of artists, then a **partial scan of the artist path** per section; falls back to all music sections ([PlexServer L78-93](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServer.cs#L78-L93), [PlexServerService L120-146](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServerService.cs#L120-L146)) |
| Lidarr | Subsonic | `startScan` on import or rename ([Subsonic.cs L31-41](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Subsonic/Subsonic.cs#L31-L41), [SubsonicServerProxy L49-51](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Subsonic/SubsonicServerProxy.cs#L49-L51)) |
| beets | Subsonic | `subsonicupdate` plugin calls `startScan` when the DB changes ([subsonicupdate.py L108-111](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/subsonicupdate.py#L108-L111)) |
| soularr | Lidarr | `DownloadedAlbumsScan` command, then polls the command status ([L846-858](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py#L846-L858)) |
| Navidrome itself | — | File watcher (`Scanner.WatcherWait` 5s; "useful to avoid scanning incomplete files"); `PurgeMissing: never` marks missing items instead of deleting them ([config options](https://www.navidrome.org/docs/usage/configuration/options/)) |
| OpenSubsonic spec | — | `startScan` "takes no extra parameters" and returns `scanStatus` ([spec](https://opensubsonic.netlify.app/docs/endpoints/startscan/)). mixsync sends `fullScan=` ([subsonic.py](../../src/mixsync/targets/subsonic.py)), which is a Navidrome extension; **unverified** against Navidrome docs |

### 2.7 Watchlist and discovery (brief)

- Lidarr: new albums are auto-monitored through `NewItemMonitorTypes.New`, fed by its own metadata server ([NewItemMonitorTypes.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Music/Model/NewItemMonitorTypes.cs)). The refresh cadence is **unverified**.
- SoulSync: follows for artists and labels with per-type rules, lookback windows and a blocklist. ListenBrainz is one of its playlist sources (README L69, L169; self-described).
- No primary source found for how either tool limits its MB load during watchlist polling. mixsync's one-request-per-check design ([watchlist](../design/watchlist.md#poller)) has no prior art to compare against here.

---

## 3. Implications for mixsync

| Area | mixsync today | Prior art | Verdict |
|---|---|---|---|
| Request unit | Release group, canonical release chosen by policy ([library](../design/library.md#release-group-canonicalization)) | Fixed editions cause unfindable releases or failed imports (soularr#11, #164) | **Matches**, and is supported by prior art |
| Ownership | MBID-keyed; `owned` without searching | Name-based ownership fails (SoulSync#101) | **Matches** |
| Pre-download ranking | Weighted features, best candidate wins, runners-up as fallback ([matching](../design/matching.md#stage-1-pre-download-candidate-scoring)) | Lidarr: lexicographic, quality first. soularr: first match wins | **Diverges on purpose.** One open point: in code each retry runs a **new search** that excludes `tried`, rather than reusing stored runners-up ([pipeline.py `search`](../../src/mixsync/acquire/pipeline.py)). Check that the docs and code agree |
| Same scorer in both stages | Yes ([ADR 0005](../decisions/0005-explainable-matching-score.md), [ADR 0006](../decisions/0006-port-beets-autotag.md)) | soularr + Lidarr disagree, which causes re-download loops | **Matches.** This is mixsync's main structural advantage |
| Ambiguity rule | Absolute thresholds per profile | beets `rec_gap_thresh` and `max_rec`; Picard min margin | **Gap.** A close runner-up (e.g. two releases in one group) can auto-accept |
| AcoustID | Mandatory; unreachable means retry | Optional or conditional elsewhere | **Stricter than prior art.** Keep it. Watch for requests stuck in "verifying" if AcoustID is down for a long time |
| Transcode / fake-lossless | Size-implied bitrate before download ([candidates.py](../../src/mixsync/match/candidates.py)) | SoulSync: post-download fake-lossless and padded-preview checks (self-described) | **Gap** after download; worth an issue |
| slskd search params | Only `searchText`; 60 × 1s poll then `TransientError` ([slskd.py `search`](../../src/mixsync/sources/slskd.py)) | soularr sets queue/speed filters and a timeout; slskd defaults to 100 responses | **Gap.** Defaults are implicit, and the timed-out search is not cancelled or deleted in slskd |
| Transfer failure | Any non-`Succeeded` completion → whole candidate out, `retry_search` ([pipeline.py](../../src/mixsync/acquire/pipeline.py)) | soularr requeues single files (×4) and treats `Rejected` as soft | **Diverges.** Consider requeuing single files for `TimedOut`/`Errored`. Remaining transfers of an abandoned candidate: does mixsync cancel them? **unverified** |
| Stall detection | `DOWNLOAD_STALL` 2h with no new bytes | soularr: wall-clock `stalled_timeout` 1h plus remote-queue timeout | **Matches** in spirit; byte-progress is the better signal |
| Retry bound | `tried` per request; `NO_RESULTS_BACKOFF` 1h → 6h → 24h → 1w | soularr denylist; SoulSync progressive backoff | **Partial.** Searches back off, but nothing caps how many candidates are downloaded and rejected per request, or the bytes they leave in `/downloads` (soularr#137 disk fill) |
| Rejected files location | Stay in `/downloads` ("never deleted", [requests.py L108-109](../../src/mixsync/acquire/requests.py)); [matching](../design/matching.md#scoring-and-bands) says "quarantine" | slskd `retention.files.complete` can delete them; soularr uses `failed_imports/` | **Gap / risk.** The docs and code differ on where quarantine is, and slskd retention can silently remove review evidence |
| File safety | Copy, fsync, hash, retag the staged copy, no-replace rename, journal, trash ([data-safety](../design/data-safety.md)) | Lidarr: size check, delete-before-move. beets: check-then-replace. SoulSync: in-place retag corruption | **Exceeds prior art.** Real incidents back each rule |
| Media server | Coalesced `startScan` after 30s ([requests.py](../../src/mixsync/acquire/requests.py)) | Lidarr batches and does partial path scans (Plex); Navidrome watcher | **Matches.** Path-scoped scans matter when the Plex/Jellyfin targets land |
| Completion signal | Polling | slskd webhooks (at-least-once, unordered) | Fine as is. Webhooks would only reduce latency and must stay idempotent |
| Upstream dependency | MB direct, behind own limiter ([ADR 0001](../decisions/0001-orchestrator-not-monolith.md), [ADR 0006](../decisions/0006-port-beets-autotag.md)) | Lidarr metadata-server outage (Lidarr#5498) | **Supported** |

---

## 4. Open questions for a grilling session

1. Should a **margin rule** (beets `rec_gap_thresh`, Picard min-margin) cap a match at *review* when the second-best release is within X distance? Should specific penalties cap the band the way beets' `max_rec` does?
2. Where does **quarantine** live: `/downloads` (owned by slskd, subject to its retention) or a mixsync-owned directory? What slskd `retention.files` values must the deploy docs require?
3. What is the **budget per request**: maximum candidates downloaded and rejected, or maximum bytes, before it goes to `failed` and needs a human? (soularr#137)
4. **Per-file requeue** before dropping a peer: which slskd terminal states are worth retrying (`TimedOut`, `Errored`), and which mean "peer is out" (`Rejected`)?
5. When a candidate is abandoned, does mixsync **cancel** its remaining slskd transfers, and **delete or cancel** timed-out searches? (slskd#1306)
6. Set slskd search parameters explicitly (`responseLimit`, `fileLimit`, `maximumPeerQueueLength`, `minimumPeerUploadSpeed`, `searchTimeout` in ms)? 100 responses may truncate searches for popular albums.
7. Should stage-1 rejects be split into **temporary** (peer busy, queue too long) and **permanent** (wrong release), like Lidarr's rejection types, so a busy peer can be retried later?
8. Add a **post-download fake-lossless or padded-preview check** (spectral or ffmpeg) as a stage-2 feature or veto? What would it cost per file?
9. If AcoustID is down for days, how long can a request stay in `verifying` before the user sees it? Is there a UI signal?
10. Do the matching docs ("runners-up are kept as fallbacks") match the code (re-search excluding `tried`)? Decide which is intended.
11. Should mixsync use **path-scoped** rescans where a target supports them (Plex now; check Navidrome), or rely on Navidrome's watcher and keep `startScan` as a backstop?

---

## 5. Sources

**Lidarr** (`aec8802`)
- [DecisionEngine/DownloadDecisionComparer.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/DecisionEngine/DownloadDecisionComparer.cs) · [DownloadDecisionMaker.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/DecisionEngine/DownloadDecisionMaker.cs)
- [Identification/DistanceCalculator.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/DistanceCalculator.cs) · [IdentificationService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Identification/IdentificationService.cs)
- [CloseAlbumMatchSpecification.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Specifications/CloseAlbumMatchSpecification.cs) · [CloseTrackMatchSpecification.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackImport/Specifications/CloseTrackMatchSpecification.cs)
- [Download/CompletedDownloadService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/CompletedDownloadService.cs) · [FailedDownloadService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/FailedDownloadService.cs) · [RedownloadFailedDownloadService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Download/RedownloadFailedDownloadService.cs) · [Blocklisting/BlocklistService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Blocklisting/BlocklistService.cs)
- [MediaFiles/RecycleBinProvider.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/RecycleBinProvider.cs) · [UpgradeMediaFileService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/UpgradeMediaFileService.cs) · [TrackFileMovingService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/MediaFiles/TrackFileMovingService.cs) · [NzbDrone.Common/Disk/DiskTransferService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Common/Disk/DiskTransferService.cs)
- [Notifications/Plex/Server/PlexServer.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServer.cs) · [PlexServerService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Plex/Server/PlexServerService.cs) · [Subsonic/Subsonic.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Subsonic/Subsonic.cs) · [SubsonicServerProxy.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Notifications/Subsonic/SubsonicServerProxy.cs)
- [Music/Model/MonitorTypes.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Music/Model/MonitorTypes.cs) · [NewItemMonitorTypes.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/Music/Model/NewItemMonitorTypes.cs) · [IndexerSearch/AlbumSearchService.cs](https://github.com/Lidarr/Lidarr/blob/aec88028d036707619baacbbb840b30c2d716318/src/NzbDrone.Core/IndexerSearch/AlbumSearchService.cs)
- Issues: [#5498](https://github.com/Lidarr/Lidarr/issues/5498) (metadata server outage, contributor comments), [#5846](https://github.com/Lidarr/Lidarr/issues/5846) (user data-loss report; closed by a contributor asking for logs; the delete-before-move order is confirmed in code above)

**beets** (`18967aa`)
- [beets/autotag/match.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/autotag/match.py) · [beets/autotag/distance.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/autotag/distance.py) · [beets/config_default.yaml](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/config_default.yaml)
- [beets/ui/commands/import_/session.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/ui/commands/import_/session.py) · [beets/util/\_\_init\_\_.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beets/util/__init__.py)
- [beetsplug/chroma.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/chroma.py) · [beetsplug/subsonicupdate.py](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/beetsplug/subsonicupdate.py)
- [docs/reference/config.rst](https://github.com/beetbox/beets/blob/18967aad9d36ca29fe2a370c3eca9beae3b46749/docs/reference/config.rst)

**soularr** (`2037399`)
- [soularr.py](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/soularr.py) · [README.md](https://github.com/mrusse/soularr/blob/2037399aa57433f9032bbae09833439830d2b409/README.md) (example config, `minimum_filename_match_ratio = 0.8`, `stalled_timeout = 3600`)
- Issues (owner `mrusse` comments): [#11](https://github.com/mrusse/soularr/issues/11), [#41](https://github.com/mrusse/soularr/issues/41), [#45](https://github.com/mrusse/soularr/issues/45), [#60](https://github.com/mrusse/soularr/issues/60), [#68](https://github.com/mrusse/soularr/issues/68), [#137](https://github.com/mrusse/soularr/issues/137), [#164](https://github.com/mrusse/soularr/issues/164)

**slskd / Soulseek.NET**
- [docs/config.md](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/docs/config.md) (Data Retention, Integrations/Webhooks/Scripts)
- [Search/API/DTO/SearchRequest.cs](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Search/API/DTO/SearchRequest.cs) · [Search/API/Controllers/SearchesController.cs](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Search/API/Controllers/SearchesController.cs) · [Events/Types/Events.cs](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/src/slskd/Events/Types/Events.cs)
- Soulseek.NET [src/Options/SearchOptions.cs](https://github.com/jpdillingham/Soulseek.NET/blob/c07c187f0eee16fa4dde548120ad733f7ebe3f44/src/Options/SearchOptions.cs) · [src/TransferStates.cs](https://github.com/jpdillingham/Soulseek.NET/blob/c07c187f0eee16fa4dde548120ad733f7ebe3f44/src/TransferStates.cs)
- Issues (maintainer `jpdillingham` comments): [#598](https://github.com/slskd/slskd/issues/598), [#1144](https://github.com/slskd/slskd/issues/1144), [#1306](https://github.com/slskd/slskd/issues/1306)

**Picard** (`84862c5`)
- [picard/metadata.py](https://github.com/metabrainz/picard/blob/84862c534c7a801b726035ee445b0903f3d3fe4a/picard/metadata.py) · [picard/options.py](https://github.com/metabrainz/picard/blob/84862c534c7a801b726035ee445b0903f3d3fe4a/picard/options.py)
- [Picard docs: Matching options](https://picard-docs.musicbrainz.org/en/latest/config/options_matching.html)

**SoulSync** (`9e35006`)
- [README.md](https://github.com/Nezreka/SoulSync/blob/9e35006055a5161cbe26f3b7af010ab5f5f2b093/README.md) (self-description only)
- Issues (owner `Nezreka` comments): [#101](https://github.com/Nezreka/SoulSync/issues/101), [#186](https://github.com/Nezreka/SoulSync/issues/186), [#1000](https://github.com/Nezreka/SoulSync/issues/1000)

**Media servers**
- [Navidrome configuration options](https://www.navidrome.org/docs/usage/configuration/options/) (Scanner.*)
- [OpenSubsonic `startScan`](https://opensubsonic.netlify.app/docs/endpoints/startscan/)

**Not verified from primary sources:** Lidarr's watchlist refresh cadence; Lidarr's behaviour when an AcoustID lookup fails; Lidarr's distance model coming from beets (inferred from identical penalty names and length grace); every SoulSync implementation detail (README only); Navidrome's `fullScan` parameter on `startScan`; whether mixsync cancels an abandoned candidate's remaining slskd transfers. The Servarr wiki settings pages render with JavaScript and could not be read, so Lidarr settings are cited from code.
