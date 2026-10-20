# `deploy`

Docker Compose on a home server, next to slskd and Navidrome. Files: `Dockerfile`, `compose.yml` (web, worker, a one-shot `mixsync-migrate` they wait for, slskd, navidrome), `.env.example` (every var, commented). Rationale: [architecture](../docs/architecture.md#deployment).

- Roles: `mixsync web` (port 8080), `mixsync worker` (scalable once on Postgres), or one `mixsync all` container.
- `.github/workflows/image.yml` publishes `ghcr.io/marameowra/mixsync:dev` on every push to `dev`; approved releases from `main` publish `:<version>` and `:latest`.

## Mounts
```
mixsync-*:  /config (rw)  /downloads (rw)  /data (rw)  /import-source (ro, migration only)  /drop (rw, optional)
slskd:      /downloads (rw)  /data/library (ro)
navidrome:  /data/library (ro)  /data/discover (ro)
```
- `/data` is one mount holding `library/`, `discover/`, `.trash/`, `.incoming/`, `quarantine/`. Never split it ([why](../docs/architecture.md#deployment)); MixSync refuses a cross-device move.
- Discovery needs Navidrome 0.58+ (multiple libraries): add `/data/discover` as Library 2 and grant the relevant users access.
- `/drop` (set `MIXSYNC_DROP_DIR=/drop`): album folders are identified every few minutes and imported or sent to review; imported originals move to `/data/.trash`. Files under a minute old are left alone; a rejected folder is not requeued unless its contents change.
- Run all containers with the same `PUID`/`PGID`.

## Environment variables
| Var | Purpose |
|---|---|
| `MIXSYNC_DATABASE_URL` | default `sqlite:////config/mixsync.db` |
| `MIXSYNC_TIMEZONE` | IANA zone for migration report dates; default `UTC` |
| `MIXSYNC_SECRET_KEY` | Fernet key for linked-account secrets; else `/config/secret.key`, generated on first start. Env wins ([details](../docs/decisions/0010-secret-key-source.md)) |
| `MIXSYNC_MB_CONTACT` | contact in the MusicBrainz User-Agent; defaults to the project URL |
| `MIXSYNC_MB_BASE_URL` | local MusicBrainz mirror |
| `MIXSYNC_MB_RATE` | MB requests/s, above 0 and at most 1 (default 1) |
| `MIXSYNC_TRASH_DAYS` | trash purge age and undo window (default 30; [data safety](../docs/design/data-safety.md)) |
| `MIXSYNC_MAX_CONCURRENT_DOWNLOADS` | active slskd transfer cap (default 3) |
| `MIXSYNC_SLOW_DOWNLOAD_WINDOW`, `_FLOOR`, `_SHARE` | when a slow transfer stops counting: seconds (60), bytes/s (50000), throughput share (0.10); [services](../docs/design/services-etiquette.md#soulseek) |
| `MIXSYNC_ACOUSTID_APP_KEY`, `MIXSYNC_ACOUSTID_USER_KEY` | AcoustID keys; the user key is optional ([rules](../docs/design/services-etiquette.md#acoustid)) |
| `MIXSYNC_SLSKD_URL`, `MIXSYNC_SLSKD_API_KEY` | slskd connection |
| `MIXSYNC_BACKUP_DIR` | SQLite backup folder (default `/config/backups`) |
| `MIXSYNC_DROP_DIR` | drop folder path in the container; unset = off |
| `MIXSYNC_ADOPT_DOWNLOADS` | `true`: album folders in `/downloads` MixSync did not request are identified and imported or sent to review; a verified import deletes the originals. Default off. Files MixSync downloaded itself are never adopted |
| `MIXSYNC_ADOPT_SETTLE_MINUTES` | idle time before a folder is adopted (default 10); raise it if peers queue the rest of a hand download longer |
| `MIXSYNC_NAVIDROME_URL`, `_USER`, `_PASSWORD` | Navidrome base URL and an **admin** account (`startScan` needs admin) |
| `PUID`, `PGID` | file ownership |
| `FORWARDED_ALLOW_IPS` | proxy addresses uvicorn trusts for `X-Forwarded-*`; default `127.0.0.1,::1` |

### Behind a TLS reverse proxy
With `FORWARDED_ALLOW_IPS` set to the proxy, the session cookie gets `Secure` and the login rate limit sees the real client IP.
1. **First stop publishing 8080**: remove `ports:` from `mixsync-web` or bind `127.0.0.1:8080:8080`. A published port can reach the container from the bridge gateway (inside the compose subnet), letting a client forge `X-Forwarded-For` (a fresh login-limit bucket per request) and `X-Forwarded-Proto`.
2. Set `FORWARDED_ALLOW_IPS` to the proxy's fixed IP (static `ipv4_address`). A subnet (`docker network inspect`, or pin with `ipam`) is looser; a subnet or `*` is safe only when 8080 is reachable solely through the proxy. IPs or CIDRs only: uvicorn does no DNS lookup. `mixsync all` reads the same variable from `.env`.

## Health and ops
- `GET /healthz` backs the web healthcheck; the worker has none yet.
- Back up `/config` and `/data` with your host backup. Upgrade: pull → `mixsync migrate` (Alembic) → restart.

- SQLite is backed up nightly to `/config/backups` (`MIXSYNC_BACKUP_DIR`; newest 7 kept) and before any `mixsync migrate` that upgrades an existing database. `mixsync backup` takes one now.
- On Postgres none of this runs; back up with `pg_dump`. `mixsync backup` exits non-zero saying so.
