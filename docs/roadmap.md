# Roadmap

## v1 goal: fully replace soulsync
v1 is done when daily use no longer needs soulsync.

### In v1
- Soulseek (via slskd) → verify → import → Navidrome
- Release-group canonicalization + fragmentation report
- Unverified import + MusicBrainz submission
- Re-import of the existing library into a new tree
- Artist/label watchlist
- Discovery playlists + retention for not-yet-owned tracks
- Users with per-user matching profiles (`admin` and `user` roles only)

### Deferred (after v1, in this order)
1. Approval queue + full role editor
2. More sources: Prowlarr + qBittorrent, Prowlarr + SABnzbd, yt-dlp
3. More targets: Jellyfin, Plex, Music Assistant
4. Stats page (Soulseek uploads, share data, torrent ratios)
5. Last.fm as a discovery source, only if its terms risk is resolved ([why](design/services-etiquette.md#lastfm))

### Explicitly out of scope
- Rebuilding a Soulseek client, chat, or rooms. slskd's UI covers these.
- Rebuilding a streaming server or player.
- Any feature that doesn't serve request → acquire → verify → organize → stream → discover.

## Build order
Estimates are solo weekends.

| # | Phase | Weekends | Contents |
|---|---|---|---|
| 1 | Skeleton | 1–2 | uv project, CI, config, DB + Alembic, form login, users + capability model, job queue, operation journal, rate limiter + cache, compose file |
| 2 | Acquire + import | 3–5 | port beets' autotag scoring ([ADR 0006](decisions/0006-port-beets-autotag.md)), MB/AcoustID clients, slskd adapter, scorer + profiles, safe importer, path templates, review UI, Navidrome rescan |
| 3 | Canonicalization + unverified + MB submit | 2–3 | Canonical release policy, fragmentation report, unverified flow, seed + Harmony links, re-check job |
| 4 | Migration re-import | 1 | Preflight, batch import, report. Mostly reuses phase 2. |
| 5 | Watchlist | 1 | Follows, poller, auto-requests |
| 6 | Discovery + retention | 2–3 | ListenBrainz link, generator, provisional library, keep/expire, playlist push |

**v1 total: about 10–15 weekends.** Phase 2 is the largest and riskiest; matching quality is the main reason MixSync exists.

## Phase exit criteria
| Phase | Done when |
|---|---|
| 1 | CI is green; you can log in with a password manager; a no-op job survives a worker restart |
| 2 | A requested album auto-imports with correct MBIDs and appears in Navidrome; a bad file lands in review; killing the worker mid-import loses nothing |
| 3 | A second release of an owned album files under the canonical one; a non-MB track imports as unverified and produces a working MB seed |
| 4 | The old library re-imports with a report; the old tree is byte-identical afterwards |
| 5 | A new release from an active followed artist creates a request within one check interval (≤ 4.5 days with the 3-day cadence and jitter); each check is a single MB request |
| 6 | A weekly playlist with unowned tracks appears in Navidrome; a starred track is promoted; an expired one moves to `.trash` |
