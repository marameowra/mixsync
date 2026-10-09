# Architecture

## Overview
MixSync is an **orchestrator**. It owns the decisions: requests, matching, library policy, playlists, retention, users, and the UI. It delegates the heavy lifting to mature tools over their APIs. See [ADR 0001](decisions/0001-orchestrator-not-monolith.md).

```
            ┌──────────────── MixSync ────────────────┐
 user ──▶   │ web (FastAPI + HTMX)                     │
            │   │                                       │
            │   ▼                                       │
            │ jobs table ◀──▶ worker(s)                 │
            │   requests · match · import · watchlist   │
            │   discovery · retention · submit          │
            └───┬─────────┬──────────┬─────────┬───────┘
                │         │          │         │
             slskd   MusicBrainz  Navidrome  ListenBrainz
           (Soulseek) AcoustID    (Subsonic)
                      CoverArt
                │                    ▲
                ▼                    │
           /downloads ──import──▶ /data/library  (plain tagged files)
```

## Pipeline
1. **Request**: a user asks for an artist, album, or track. Requests also come from the [watchlist](design/watchlist.md) and [discovery](design/discovery-retention.md).
2. **Search + score**: query slskd and score candidates *before* downloading ([matching](design/matching.md), stage 1).
3. **Download**: slskd transfers into `/downloads`.
4. **Verify**: fingerprint and score against MusicBrainz ([matching](design/matching.md), stage 2). The result is one of three bands: auto-accept, review, or reject.
5. **Import**: copy, tag, and file into `/data/library` under the canonical release ([library](design/library.md), [data safety](design/data-safety.md)).
6. **Publish**: trigger a Navidrome rescan and push playlists.

## Components and delegation
| Concern | Delegate to | MixSync's job |
|---|---|---|
| Soulseek | **slskd** (REST API; shares, rooms, chat, transfers) | Search strategy, candidate scoring, queueing. Chat and rooms are used through slskd's own UI, not rebuilt. |
| Fingerprinting | **Chromaprint `fpcalc`** (bundled in image) + **AcoustID** API | Gather match evidence |
| Metadata | **MusicBrainz** WS/2 JSON API, **Cover Art Archive** | Typed client, caching, rate limiting |
| Tag I/O | **mutagen** | Snapshot original tags before every write |
| Matching heuristics | **beets** `autotag` distance model (MIT), ported into `match/` ([ADR 0006](decisions/0006-port-beets-autotag.md)) | Explainable scorer, per-user thresholds, calibration |
| Streaming | **Navidrome** (Subsonic API) | Rescan, playlist push, read stars and plays per user |
| Recommendations | **ListenBrainz** (`liblistenbrainz`). Last.fm is deferred. | Per-user playlist generation |
| MB submission | **MB release-editor seeding**, **Harmony** | Build the seed, open it in the browser |
| Studied, not adopted | Lidarr, Soularr, Explo, Picard | UX and logic reference |

## Adapter interfaces
Every external system sits behind a small interface, so new sources and targets plug in without touching core code.

| Interface | v1 implementation | Later |
|---|---|---|
| `DownloadSource` | slskd | Prowlarr + qBittorrent, Prowlarr + SABnzbd, yt-dlp |
| `MetadataProvider` | MusicBrainz, AcoustID, Cover Art Archive | Discogs, Deezer (read-only fallbacks) |
| `LibraryTarget` | Navidrome | Jellyfin, Plex, Music Assistant |
| `HistorySource` | ListenBrainz, Navidrome plays | Last.fm (deferred) |

## Tech stack
Rationale lives in [ADR 0002](decisions/0002-python-backend.md), [0003](decisions/0003-htmx-server-rendered-ui.md), and [0004](decisions/0004-sqlite-and-db-job-queue.md).

- **Runtime/tooling:** Python 3.13, uv, ruff, pyright (strict), pre-commit
- **Web:** FastAPI, pydantic v2, Jinja2, HTMX, sse-starlette; session-cookie auth, argon2 password hashes
- **Data:** SQLAlchemy 2 + Alembic. SQLite (WAL mode) by default, Postgres-ready. Job queue is a DB table with leased workers.
- **HTTP:** httpx (async), a persistent token bucket per service, on-disk response cache (hishel or a DB table)
- **Music:** mutagen, pyacoustid + fpcalc, rapidfuzz, slskd REST via httpx

## Users and permissions
- Local accounts with a standard HTML login form (`autocomplete="username"` / `"current-password"`) so password managers work. OIDC and passkeys are possible later.
- **Capability-based roles.** A role is a bundle of capabilities: `request`, `download.auto` (skip approval), `approve`, `library.edit`, `workflow.edit`, `admin`.
- v1 ships with only `admin` and `user` roles. The capability model exists in the schema from day one because discovery is per-user and retrofitting users is painful. The approval queue and role editor come after v1.
- Each user links to their Navidrome account (for stars and plays) and optionally to ListenBrainz.
- Each user has a **matching profile** (strict / balanced / loose) that sets their auto-accept and review thresholds.

## Repo layout
```
src/mixsync/
  core/        domain models, job state machine, operation journal, config
  db/          SQLAlchemy models, Alembic migrations
  ratelimit/   token buckets + HTTP cache
  sources/     slskd.py                 (DownloadSource)
  metadata/    musicbrainz.py, acoustid.py, coverart.py, fallback/
  match/       features.py, scorer.py, profiles.py, calibrate.py
  library/     paths.py, fileops.py, tags.py, canonical.py, importer.py
  targets/     navidrome.py             (LibraryTarget)
  discovery/   listenbrainz.py, playlists.py, retention.py
  watchlist/   poller.py
  submit/      mb_seed.py, harmony.py
  web/         routes/, templates/, static/app.css
  worker.py, app.py
tests/
  unit/, integration/, fixtures/audio/, cassettes/
deploy/        Dockerfile, compose.yml, .env.example (spec in README)
docs/
```

Every folder has a `README.md` with implementation guidance: purpose, boundaries, proposed files, interfaces, rules, and allowed imports. Start with the [package overview](../src/mixsync/README.md), which also defines the module dependency rules.

## Deployment
**Target:** Docker Compose on a home server, next to slskd and Navidrome.

- One image, two roles: `mixsync web` and `mixsync worker`. Small installs can run both in one container. Larger installs run separate worker containers against Postgres.
- `fpcalc` is bundled in the image.

| Mount / path | Purpose | Mode |
|---|---|---|
| `/config` | DB, settings, backups | rw |
| `/downloads` | Shared with slskd | rw |
| `/data` | **One mount** holding the four folders below | rw |
| `/data/library` | Main library (Navidrome library 1) | |
| `/data/discover` | Provisional discovery tracks (Navidrome library 2) | |
| `/data/.trash` | Soft-deleted files, purged after N days | |
| `/data/.incoming` | Staging for atomic imports | |
| `/import-source` | Old library during [migration](design/migration.md) | **ro** |

**Why one `/data` mount:** moving a file between library, discover, trash, and incoming must be an atomic `rename`, and `rename` fails across Docker mount points (`EXDEV`). Keeping `discover/` as a sibling of `library/`, not nested inside it, also stops Navidrome's main library from indexing provisional tracks. Navidrome mounts only `/data/library` and `/data/discover`.

> [!question] Navidrome multi-library
> Confirm that the deployed Navidrome version supports multiple libraries (added in 0.58). If it doesn't, fall back to one library with discovery tracks identified by a tag or playlist.
