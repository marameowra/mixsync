# `deploy`

Docker Compose on a home server, next to slskd and Navidrome. Files: `Dockerfile`, `compose.build.yml` (local-build override), `compose.postgres.yml` (optional Postgres override), `compose.yml` (web, worker, a one-shot `mixsync-migrate` they wait for, slskd, navidrome), `.env.example` (every var, commented). Rationale: [architecture](../docs/architecture.md#deployment).

- Roles: `mixsync web` (port 8080), `mixsync worker` (scalable once on Postgres), or one `mixsync all` container.
- `.github/workflows/image.yml` publishes `ghcr.io/marameowra/mixsync:dev` on every push to `dev`; approved releases from `main` publish `:<version>` and `:latest`.

## Mounts
```
mixsync-*:   /config (rw)  /downloads (rw)  /data (rw)  /import-source (ro, migration only)  /drop (rw, optional)  /torrents (ro, optional)  /usenet (rw, optional)
slskd:       /downloads (rw)  /data/library (ro)
navidrome:   /data/library (ro)  /data/discover (ro)
qbittorrent: /torrents (rw, optional)
sabnzbd:     /usenet (rw, optional)
```
- `/data` is one mount holding `library/`, `discover/`, `.trash/`, `.incoming/`, `quarantine/`. Never split it ([why](../docs/architecture.md#deployment)); MixSync refuses a cross-device move.
- Discovery needs Navidrome 0.58+ (multiple libraries): add `/data/discover` as Library 2, name it `discover` (or set `MIXSYNC_NAVIDROME_DISCOVER_LIBRARY`), and grant the relevant users access.
- `/drop` (set `MIXSYNC_DROP_DIR=/drop`): album folders are identified every few minutes and imported or sent to review; imported originals move to `/data/.trash`. Files under a minute old are left alone; a rejected folder is not requeued unless its contents change.
- `/torrents` (set `MIXSYNC_QBITTORRENT_DIR=/torrents`): qBittorrent's save path for MixSync's torrents (category `mixsync`), at the same path in both containers and apart from `/downloads`, `/data` and `/drop`. MixSync only copies from it ([ADR 0022](../docs/decisions/0022-seeding-downloads-copied-never-released.md)); qBittorrent's ratio and seed-time rules remove finished torrents, so an album takes disk twice until then.
- `/usenet` (set `MIXSYNC_SABNZBD_DIR=/usenet`): the folder of SABnzbd's `mixsync` category, at the same path in both containers and apart from `/downloads`, `/data`, `/drop` and `/torrents`. Usenet downloads do not seed: after a verified import MixSync deletes them there (journaled), as it does in `/downloads`.
- Run all containers with the same `PUID`/`PGID`.

## Environment variables
| Var | Purpose |
|---|---|
| `MIXSYNC_DATABASE_URL` | default `sqlite:////config/mixsync.db`; or Postgres, see [Postgres](#postgres) |
| `MIXSYNC_TIMEZONE` | IANA zone for migration report dates; default `UTC` |
| `MIXSYNC_SECRET_KEY` | Fernet key for linked-account secrets; else `/config/secret.key`, generated on first start. Env wins ([details](../docs/decisions/0010-secret-key-source.md)) |
| `MIXSYNC_MB_CONTACT` | contact in the MusicBrainz User-Agent; defaults to the project URL |
| `MIXSYNC_MB_BASE_URL` | local MusicBrainz mirror |
| `MIXSYNC_MB_RATE` | MB requests/s, above 0 and at most 1 (default 1) |
| `MIXSYNC_TRASH_DAYS` | trash purge age and undo window (default 30; [data safety](../docs/design/data-safety.md)) |
| `MIXSYNC_MAX_CONCURRENT_DOWNLOADS` | active slskd transfer cap (default 3) |
| `MIXSYNC_SLOW_DOWNLOAD_WINDOW`, `_FLOOR`, `_SHARE` | when a slow transfer stops counting: seconds (60), bytes/s (50000), throughput share (0.10); [services](../docs/design/services-etiquette.md#soulseek) |
| `MIXSYNC_ACOUSTID_APP_KEY`, `MIXSYNC_ACOUSTID_USER_KEY` | AcoustID keys; the user key is optional ([rules](../docs/design/services-etiquette.md#acoustid)) |
| `MIXSYNC_DEEZER_SUGGESTIONS` | `true`: Mark unverified gets a "Suggest from Deezer" button that pre-fills the form on request (metadata only, 1 req/s, no audio). Deezer's terms allow private, non-commercial, single-household use only. Default off |
| `MIXSYNC_DISCOGS_SUGGESTIONS` | `true`: Discogs joins Deezer behind one "Suggest" button on Mark unverified (release search and tracklist, on request only). Each user links their own Discogs personal token under Account (no shared token); a user without one gets no Discogs suggestions. The limits in [services](../docs/design/services-etiquette.md#discogs-and-deezer-fallbacks-off-by-default) came from an OpenAPI copy: check them by hand before enabling. Default off |
| `MIXSYNC_SLSKD_URL`, `MIXSYNC_SLSKD_API_KEY` | slskd connection |
| `MIXSYNC_SLSKD_MAX_SEARCHES` | slskd searches in flight across all workers (default 2); extra searches wait |
| `MIXSYNC_SLSKD_SEARCH_DEBOUNCE` | seconds a search query's result is reused (default 300) |
| `MIXSYNC_PROWLARR_URL`, `MIXSYNC_PROWLARR_API_KEY` | Prowlarr connection (torrent and Usenet search, 1 request / 2 s). With qBittorrent set too, torrents are a download source; with SABnzbd, Usenet; either is enough |
| `MIXSYNC_QBITTORRENT_URL`, `MIXSYNC_QBITTORRENT_USERNAME`, `MIXSYNC_QBITTORRENT_PASSWORD` | qBittorrent Web UI and login (empty if its auth is bypassed for MixSync); also on the settings page |
| `MIXSYNC_QBITTORRENT_DIR` | save path of MixSync's torrents, the same in both containers; unset = no torrents |
| `MIXSYNC_SABNZBD_URL`, `MIXSYNC_SABNZBD_API_KEY` | SABnzbd connection and API key; also on the settings page, never logged |
| `MIXSYNC_SABNZBD_DIR` | folder of SABnzbd's `mixsync` category, the same in both containers; unset = no Usenet |
| `MIXSYNC_BACKUP_DIR` | SQLite backup folder (default `/config/backups`); not used on Postgres |
| `MIXSYNC_DROP_DIR` | drop folder path in the container; unset = off |
| `MIXSYNC_ADOPT_DOWNLOADS` | `true`: album folders in `/downloads` MixSync did not request are identified and imported or sent to review; a verified import deletes the originals. Default off. Files MixSync downloaded itself are never adopted |
| `MIXSYNC_ADOPT_SETTLE_MINUTES` | idle time before a folder is adopted (default 10); raise it if peers queue the rest of a hand download longer |
| `MIXSYNC_NAVIDROME_LIBRARY` | name of the main Navidrome library (default `Music Library`) |
| `MIXSYNC_NAVIDROME_DISCOVER_LIBRARY` | name of the discover Navidrome library (default `discover`) |
| `MIXSYNC_NAVIDROME_URL`, `_USER`, `_PASSWORD` | Navidrome base URL and an **admin** account (`startScan` needs admin) |
| `PUID`, `PGID` | file ownership |
| `FORWARDED_ALLOW_IPS` | proxy addresses uvicorn trusts for `X-Forwarded-*`; default `127.0.0.1,::1` |

### Behind a TLS reverse proxy
With `FORWARDED_ALLOW_IPS` set to the proxy, the session cookie gets `Secure` and the login rate limit sees the real client IP.
1. **First stop publishing 8080**: remove `ports:` from `mixsync-web` or bind `127.0.0.1:8080:8080`. A published port can reach the container from the bridge gateway (inside the compose subnet), letting a client forge `X-Forwarded-For` (a fresh login-limit bucket per request) and `X-Forwarded-Proto`.
2. Set `FORWARDED_ALLOW_IPS` to the proxy's fixed IP (static `ipv4_address`). A subnet (`docker network inspect`, or pin with `ipam`) is looser; a subnet or `*` is safe only when 8080 is reachable solely through the proxy. IPs or CIDRs only: uvicorn does no DNS lookup. `mixsync all` reads the same variable from `.env`.

## Postgres
SQLite is the default and fits one home server. Choose Postgres to run several workers or to use your own database tooling.
1. In `.env` set `POSTGRES_PASSWORD` and `MIXSYNC_DATABASE_URL=postgresql://mixsync:<POSTGRES_PASSWORD>@postgres:5432/mixsync` (URL-encode special characters in the password). `postgresql+psycopg://` also works; other drivers such as `+psycopg2` are rejected at startup.
2. Create the data dir as your own user so `PUID:PGID` owns it (Docker would make it root-owned and Postgres could not start): `mkdir -p "$CONFIG_DIR/postgres"`.
3. Start with the override: `docker compose -f deploy/compose.yml -f deploy/compose.postgres.yml up -d`. Add the same `-f` flags to every later `docker compose` command. Migrations run on the new database; existing SQLite data is not copied over.
4. Backups are yours: mixsync's nightly backup is SQLite-only. Example: `docker compose -f deploy/compose.yml -f deploy/compose.postgres.yml exec postgres pg_dump -U mixsync mixsync > mixsync.sql`. The data lives in `${CONFIG_DIR}/postgres`.

## Health and ops
- `GET /healthz` backs the web healthcheck. The worker records a heartbeat every 10 s while idle (and during long jobs); `mixsync worker-health` exits non-zero when none is newer than 90 s and backs the worker healthcheck. Admins see "worker last seen" under Account → Worker status. It catches a dead or wedged worker loop, not a single job hung on a network call that never returns.
- Logs go to stderr (timestamp, level, logger, message); `MIXSYNC_LOG_LEVEL` sets the level (default `INFO`).
- Back up `/config` and `/data` with your host backup. Upgrade: see below.

- SQLite is backed up nightly to `/config/backups` (`MIXSYNC_BACKUP_DIR`; newest 7 kept) and before any `mixsync migrate` that upgrades an existing database. `mixsync backup` takes one now.
- On Postgres none of this runs; back up with `pg_dump`. `mixsync backup` exits non-zero saying so.

### Seed or recover settings

These commands work with either database, after migrations. Export settings into the
mounted config directory, preview the import on the destination, then apply it:

```sh
docker compose -f deploy/compose.yml exec -T mixsync-web mixsync export-settings --output /config/settings.json
docker compose -f deploy/compose.yml exec -T mixsync-web mixsync import-settings /config/settings.json
docker compose -f deploy/compose.yml exec -T mixsync-web mixsync import-settings /config/settings.json --apply
```

Copy `settings.json` privately to the destination's config directory before importing.
If the destination has no active admin, first run
`docker compose -f deploy/compose.yml exec mixsync-web mixsync user-add recovery-admin --admin`
with a username absent from the export; this account lets you enable imported users.
Export refuses to overwrite an existing output path. Import only adds missing rows;
existing users, settings, follows, playlists, and links are skipped, never overwritten.
Repeated imports are safe. This transfers settings, not music, request history, or the
operation journal; retain normal database and host backups. Mirrored and smart
playlists are not exported yet (#401); recreate them after an import.
If app settings were added, run
`docker compose -f deploy/compose.yml restart mixsync-web mixsync-worker` to reload their
startup settings. The settings page's restart warning tracks the imported version.

Users imported without password hashes are disabled. Set their passwords on the Users
page and enable them. To preserve passwords, use `--include-secrets` on both export and
import; exports created with `--output` have mode `0600`. Linked-account tokens and
saved app credentials are always excluded: relink accounts and configure credentials
on the destination. See [settings data safety](../docs/design/data-safety.md#settings-export-and-import).

## Upgrading
`compose.yml` runs `ghcr.io/marameowra/mixsync:${MIXSYNC_TAG:-latest}`. Pin `MIXSYNC_TAG` in `.env` to a version (e.g. `1.0.1`) or leave it at `latest`.
1. Back up: `docker compose -f deploy/compose.yml run --rm mixsync-migrate backup` (SQLite; on Postgres use `pg_dump`).
   Coming from the old local-build compose? There is no ghcr image yet, so take this first backup with `-f deploy/compose.build.yml` added.
2. Set `MIXSYNC_TAG` to the new version, then `docker compose -f deploy/compose.yml pull`.
3. `docker compose -f deploy/compose.yml up -d`. The one-shot `mixsync-migrate` service runs migrations first; web and worker start after it succeeds.
4. Check: `docker compose -f deploy/compose.yml ps`, `docker compose -f deploy/compose.yml logs mixsync-migrate`, and `GET /healthz`.

To roll back, restore the backup and set the previous tag.

### Building locally (contributors)
`docker compose -f deploy/compose.yml -f deploy/compose.build.yml up -d --build` builds from the repo `Dockerfile` and tags it `mixsync:local`.

### Bumping slskd and Navidrome
Both are pinned in `compose.yml` (slskd `0.26.2`, Navidrome `0.64.2`; Navidrome must stay 0.58+). To bump: read the release notes ([slskd](https://github.com/slskd/slskd/releases), [Navidrome](https://github.com/navidrome/navidrome/releases)), back up `/config`, change the tag in `compose.yml` (Docker tags have no `v` prefix), then `pull` and `up -d`.
