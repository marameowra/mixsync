# `deploy`

How MixSync runs: Docker Compose on a home server, next to slskd and Navidrome. The real `compose.yml`, `Dockerfile`, and `.env.example` are phase-1 deliverables; this README is their spec.

## Proposed files
| File | Contents |
|---|---|
| `Dockerfile` | Python 3.13 slim + `uv sync --frozen` + `chromaprint` (`fpcalc`); non-root user; one entrypoint `mixsync <role>` |
| `compose.yml` | The services below |
| `.env.example` | Every env var with a comment, no real values |

## Services
| Service | Image | Notes |
|---|---|---|
| `mixsync-web` | this repo | `mixsync web`; port 8080 |
| `mixsync-worker` | this repo | `mixsync worker`; scale with `--scale` once on Postgres |
| `slskd` | `slskd/slskd` | shares `/downloads`; shares the library back read-only |
| `navidrome` | `deluan/navidrome` | libraries `/data/library` and `/data/discover`, both read-only |

Small installs can run a single `mixsync all` container instead of web + worker.

`.github/workflows/image.yml` publishes the image as `ghcr.io/marameowra/mixsync:dev` on every push to `dev`. Approved releases from `main` publish `:<version>` and `:latest`.

## Mounts
```
mixsync-*:   /config  (rw)   /downloads (rw)   /data (rw)   /import-source (ro, migration only)
slskd:       /downloads (rw) /data/library (ro, shared folder)
navidrome:   /data/library (ro)  /data/discover (ro)
```
- **`/data` is one mount**, so `rename` between `library/`, `discover/`, `.trash/`, and `.incoming/` stays atomic. Never split it into separate mounts ([architecture](../docs/architecture.md#deployment)).
- **Discovery library:** needs Navidrome 0.58+ (multiple libraries). Create `${DATA_DIR}/discover` on the same filesystem as `data/.incoming` and `data/.trash`, add `/data/discover` as Library 2 in Navidrome, and grant users access to it.
- `/import-source` is read-only at the OS level during [migration](../docs/design/migration.md).
- **File ownership:** run all containers with the same `PUID`/`PGID` so files written by MixSync are readable by Navidrome and slskd.

## Environment variables
| Var | Purpose |
|---|---|
| `MIXSYNC_DATABASE_URL` | default `sqlite:////config/mixsync.db` |
| `MIXSYNC_TIMEZONE` | IANA zone (e.g. `America/Los_Angeles`) in which the migration report reads its dates. Default `UTC` |
| `MIXSYNC_SECRET_KEY` | Fernet key for linked-account secrets. Optional: if unset, `/config/secret.key` is used, or generated on first start. The env var wins if both exist ([details](../src/mixsync/db/README.md#conventions)). |
| `MIXSYNC_MB_CONTACT` | contact for the MusicBrainz User-Agent. Optional; defaults to the project's GitHub URL |
| `MIXSYNC_MB_BASE_URL` | optional local MusicBrainz mirror |
| `MIXSYNC_ACOUSTID_APP_KEY` | AcoustID application key |
| `MIXSYNC_ACOUSTID_USER_KEY` | AcoustID user key (from your acoustid.org account). Optional: without it fingerprints are not submitted after a promotion. All submissions go under this one account. |
| `MIXSYNC_SLSKD_URL`, `MIXSYNC_SLSKD_API_KEY` | slskd connection |
| `MIXSYNC_NAVIDROME_URL` | Navidrome base URL |
| `MIXSYNC_NAVIDROME_USER`, `MIXSYNC_NAVIDROME_PASSWORD` | a Navidrome **admin** account, used to start library rescans after imports (`startScan` needs admin) |
| `PUID`, `PGID` | file ownership |
| `FORWARDED_ALLOW_IPS` | proxy addresses uvicorn trusts for `X-Forwarded-*`. Default `127.0.0.1,::1`; see below |

### Behind a TLS reverse proxy
With `FORWARDED_ALLOW_IPS` set to the proxy's address, the session cookie gets `Secure` (the scheme comes from `X-Forwarded-Proto`) and the login rate limit sees the real client IP instead of the proxy's.

1. **First stop publishing 8080**: remove `ports:` from `mixsync-web`, or bind `127.0.0.1:8080:8080`. Traffic to a published port can reach the container from the bridge gateway (e.g. `172.18.0.1`, via docker-proxy, rootless Docker, Docker Desktop), which is inside the compose subnet. A client that gets in that way could forge `X-Forwarded-For` (a fresh login-limit bucket per request) and `X-Forwarded-Proto`.
2. Then set `FORWARDED_ALLOW_IPS` to the proxy container's fixed IP (give it a static `ipv4_address`). A whole subnet is looser; find it with `docker network inspect <network>` or pin it with compose `ipam`. Use IPs or CIDRs only: hostnames do not work, since uvicorn compares the string to the client IP with no DNS lookup.

A subnet, or `*`, is only safe when 8080 is not reachable except through the proxy. `mixsync all` also runs uvicorn and reads the same variable from `.env`.

## Health and ops
- `GET /healthz` (web) and a worker heartbeat row; compose `healthcheck` on both.
- Backups land in `/config/backups`. Back up `/config` and `/data` with your normal host backup.
- Upgrades: pull → `mixsync migrate` runs Alembic (with a pre-migration DB backup) → restart.

## Design docs
[Architecture: deployment](../docs/architecture.md#deployment) · [Data safety](../docs/design/data-safety.md)
