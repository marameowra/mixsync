# Architecture

## Overview
MixSync is an **orchestrator**. It owns the decisions: requests, matching, library policy, playlists, retention, users, and the UI. It delegates the heavy lifting to mature tools over their APIs. See [ADR 0001](decisions/0001-orchestrator-not-monolith.md).

```
            ┌──────────────── MixSync ────────────────┐
 user ──▶   │ web (FastAPI + Jinja2)                   │
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
| Fingerprinting | **Chromaprint `fpcalc`** (bundled in image) + **AcoustID** HTTP API | Gather match evidence |
| Metadata | **MusicBrainz** WS/2 JSON API, **Cover Art Archive** | Typed client, caching, rate limiting |
| Tag I/O | **mutagen** | Snapshot original tags before every write |
| Matching heuristics | **beets** `autotag` distance model (MIT), ported into `match/` ([ADR 0006](decisions/0006-port-beets-autotag.md)) | Explainable scorer, per-user thresholds, calibration |
| Streaming | **Navidrome** (Subsonic API) | Rescan, playlist push, read stars and plays per user |
| Recommendations | **ListenBrainz** (httpx client). Last.fm is deferred ([why](design/services-etiquette.md#lastfm)). | Per-user playlist generation |
| MB submission | **MB release-editor seeding**, **Harmony** | Build the seed, open it in the browser |
| Studied, not adopted | Lidarr, Soularr, Explo, Picard | UX and logic reference |

## Adapter interfaces
Every external system sits behind a small interface, so new sources and targets plug in without touching core code.

| Interface | v1 implementation | Later |
|---|---|---|
| `DownloadSource` | slskd | Prowlarr + qBittorrent, Prowlarr + SABnzbd, yt-dlp |
| `MetadataProvider` | MusicBrainz, AcoustID, Cover Art Archive | Discogs, Deezer (read-only fallbacks) |
| `LibraryTarget` | Navidrome | Jellyfin, Plex, Music Assistant |
| `HistorySource` | ListenBrainz, Navidrome plays | |

## Tech stack
Rationale: [ADR 0002](decisions/0002-python-backend.md), [0003](decisions/0003-htmx-server-rendered-ui.md), [0004](decisions/0004-sqlite-and-db-job-queue.md). Versions are in `pyproject.toml`.

- **Runtime/tooling:** Python 3.13, uv, ruff, pyright (strict), import-linter
- **Web:** FastAPI, uvicorn, pydantic v2 / pydantic-settings, Jinja2, session-cookie auth, argon2 password hashes, Fernet (`cryptography`) for linked-account secrets
- **Data:** SQLAlchemy 2 + Alembic. SQLite (WAL) by default, Postgres supported. The job queue is a DB table with leased workers.
- **HTTP:** httpx; a persistent per-service token bucket and response cache in the DB (`ratelimit/`)
- **Music:** mutagen, rapidfuzz, `fpcalc` and slskd via httpx

> Planned — not built (#221): HTMX and SSE progress streams on the server-rendered pages ([ADR 0003](decisions/0003-htmx-server-rendered-ui.md)).

## Users and permissions
- Local accounts with a standard HTML login form (`autocomplete="username"` / `"current-password"`) so password managers work. OIDC and passkeys are possible later.
- **Capability-based roles.** A role is a bundle of capabilities: `request`, `download.auto` (skip approval), `approve`, `library.edit`, `workflow.edit`, `admin`.
- v1 ships with only `admin` and `user` roles. The capability model exists in the schema from day one because discovery is per-user and retrofitting users is painful. The approval queue and role editor come after v1.
- Each user links to their Navidrome account (for stars and plays) and optionally to ListenBrainz.
- Each user has a **matching profile** (strict / balanced / loose) that sets their auto-accept and review thresholds.

## Repo layout
See the [package overview](../src/mixsync/README.md): module map, entry points, and the dependency rules `import-linter` enforces. Every folder has a `README.md` with its boundaries and rules.

## Deployment
Docker Compose on a home server, next to slskd and Navidrome. One image, roles `web`, `worker` or `all`; `fpcalc` is bundled. Mounts, env vars and Navidrome setup are in [deploy/README.md](../deploy/README.md).

**Why one `/data` mount:** moving a file between library, discover, trash, quarantine and incoming must be an atomic `rename`, and `rename` fails across Docker mount points (`EXDEV`). `discover/` is a sibling of `library/`, not nested in it, so Navidrome's main library never indexes provisional tracks; Navidrome mounts only `/data/library` and `/data/discover`. `/downloads` is a separate mount shared with slskd, so release from it is copy, verify, delete ([data safety](design/data-safety.md)).
