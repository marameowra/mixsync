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
1. **Request** from a user, the [watchlist](design/watchlist.md) or [discovery](design/discovery-retention.md).
2. **Search + score** slskd candidates before downloading ([matching](design/matching.md), stage 1).
3. **Download** into `/downloads`.
4. **Verify** by fingerprint and MusicBrainz score ([matching](design/matching.md), stage 2): auto-accept, review or reject.
5. **Import** into `/data/library` under the canonical release ([library](design/library.md), [data safety](design/data-safety.md)).
6. **Publish**: Navidrome rescan and playlist push.

## Components and delegation
| Concern | Delegate to | MixSync's job |
|---|---|---|
| Soulseek | slskd (REST; chat and rooms stay in its UI) | Search strategy, scoring, queueing |
| Fingerprinting | Chromaprint `fpcalc` (in the image) + AcoustID | Gather match evidence |
| Metadata | MusicBrainz WS/2 JSON, Cover Art Archive | Typed client, caching, rate limiting |
| Tag I/O | mutagen | Snapshot original tags before every write |
| Matching | beets `autotag` distance model, ported into `match/` ([ADR 0006](decisions/0006-port-beets-autotag.md)) | Explainable scorer, per-user thresholds |
| Streaming | Navidrome (Subsonic) | Rescan, playlist push, read stars and plays |
| Recommendations | ListenBrainz; Last.fm [deferred](design/services-etiquette.md#lastfm) | Per-user playlists |
| MB submission | Release-editor seeding, Harmony | Build the seed, open it in the browser |

Studied, not adopted: Lidarr, Soularr, Explo, Picard ([research](research/workflow-prior-art.md)).

## Adapter interfaces
Each external system sits behind a small interface (`core/protocols.py`).

| Interface | v1 | Later |
|---|---|---|
| `DownloadSource` | slskd | Prowlarr + qBittorrent/SABnzbd, yt-dlp |
| `MetadataProvider` | MusicBrainz, AcoustID, Cover Art Archive | Discogs, Deezer (#223) |
| `LibraryTarget` | Navidrome | Jellyfin, Plex, Music Assistant |
| `HistorySource` | ListenBrainz, Navidrome plays | |

## Tech stack
Rationale: ADRs [0002](decisions/0002-python-backend.md), [0003](decisions/0003-htmx-server-rendered-ui.md), [0004](decisions/0004-sqlite-and-db-job-queue.md). Versions: `pyproject.toml`.

Python 3.13, uv, ruff, pyright (strict), import-linter. FastAPI, pydantic v2, Jinja2, session-cookie auth, argon2, Fernet for linked-account secrets. SQLAlchemy 2 + Alembic on SQLite (WAL) or Postgres; the job queue is a DB table with leased workers. httpx with a per-service token bucket and response cache in the DB. mutagen, rapidfuzz, `fpcalc`.

> Planned — not built (#221): HTMX live regions that poll for progress on the server-rendered pages ([ADR 0008](decisions/0008-live-updates-by-htmx-polling.md); replaces the SSE plan).

## Users and permissions
- Local accounts with a standard login form (`autocomplete="username"` / `"current-password"`) so password managers work.
- Roles bundle capabilities (`core/capabilities.py`). v1 ships only `admin` and `user`; the approval queue and role editor come later.
- Each user links a Navidrome account (stars, plays), optionally ListenBrainz, and has a matching profile (strict / balanced / loose) setting their thresholds.

## Repo layout
See the [package overview](../src/mixsync/README.md): module map, entry points, dependency rules.

## Deployment
Docker Compose next to slskd and Navidrome; mounts, env vars and Navidrome setup are in [deploy/README.md](../deploy/README.md).

**Why one `/data` mount:** moving a file between library, discover, trash, quarantine and incoming must be an atomic `rename`, and `rename` fails across Docker mount points (`EXDEV`). `discover/` is a sibling of `library/`, not nested in it, so Navidrome's main library never indexes provisional tracks; Navidrome mounts only `/data/library` and `/data/discover`. `/downloads` is a separate mount shared with slskd, so release from it is copy, verify, delete ([data safety](design/data-safety.md)).
