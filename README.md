# MixSync

A self-hosted music stack. Ask for an album, artist, or track, and MixSync finds it on
Soulseek, checks the files against MusicBrainz and AcoustID, files them into a clean
library, and tells your media server to pick them up.

MixSync orchestrates mature tools rather than replacing them: [slskd](https://github.com/slskd/slskd)
does the Soulseek networking and [Navidrome](https://www.navidrome.org/) does the streaming.

## Features

- **Request → download → verify → import.** Searches Soulseek through slskd, downloads the best
  candidate, and scores it against MusicBrainz with an explainable distance (ported from beets).
  Every file is fingerprinted with AcoustID.
- **Review queue.** Close calls, suspected transcodes, padded previews, and AcoustID
  contradictions wait for a person instead of being imported silently.
- **Clean library.** Consistent paths and tags, release-group canonicalization, and a
  fragmentation report for albums split across releases.
- **Bring your existing music.** Migrate an old library, watch a drop folder, or adopt album
  folders already in your downloads.
- **Watchlist.** Follow artists and labels; new releases are requested automatically, within a
  weekly cap.
- **Discovery playlists** built from your ListenBrainz listening, with a retention window for
  tracks you don't own yet.
- **Unverified imports.** Music missing from MusicBrainz can still be imported, and MixSync
  helps you submit it.
- **Data safety first.** Nothing is deleted silently: removals go to a trash with a journal and an
  undo window. See [data safety](docs/design/data-safety.md).
- **Multi-user.** Per-user matching profiles (strict, balanced, loose) and `admin`/`user` roles.

Not built yet: torrent and Usenet sources, Jellyfin/Plex targets. The
[issues](https://github.com/marameowra/mixsync/issues) are the roadmap.

## Setup

MixSync runs with Docker Compose next to slskd and Navidrome. You need Docker with the Compose
plugin, a Soulseek account, and an [AcoustID application key](https://acoustid.org/new-application).

1. Clone the repo:
   ```sh
   git clone https://github.com/marameowra/mixsync.git
   cd mixsync/deploy
   ```
2. Create your config and fill it in. Every variable is commented:
   ```sh
   cp .env.example .env
   ```
   At minimum set `PUID`/`PGID`, the host paths (`CONFIG_DIR`, `DOWNLOADS_DIR`, `DATA_DIR`),
   `MIXSYNC_ACOUSTID_APP_KEY`, `MIXSYNC_SLSKD_API_KEY`, and a Navidrome **admin** account.
3. Start the stack. It builds the image, runs database migrations once, then starts the web UI,
   the worker, slskd, and Navidrome:
   ```sh
   docker compose up -d
   ```
4. Create your admin account:
   ```sh
   docker compose run --rm mixsync-web user-add <username> --admin
   ```
5. Set your Soulseek login in slskd (see the [slskd docs](https://github.com/slskd/slskd/blob/master/docs/config.md)),
   then open MixSync at <http://localhost:8080>.

For discovery playlists, add `/data/discover` as a second library in Navidrome (0.58+).

Keep `/data` as **one** mount: library, trash, and quarantine must sit on the same filesystem so
moves stay atomic. Full details, including every environment variable, running behind a TLS
reverse proxy, backups, and upgrades, are in the [deploy guide](deploy/README.md).

Prebuilt images are published to `ghcr.io/marameowra/mixsync` (`:latest` for releases, `:dev`
for the development branch).

## Contributing

Bug reports and pull requests are welcome. Open an
[issue](https://github.com/marameowra/mixsync/issues) first for anything larger than a small
fix, so we can agree on the approach.

### Development setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                              # install dependencies
uv run python -m devtools.preview    # UI preview with fake services at http://127.0.0.1:8090
```

### Before you open a PR

Run every CI step. `scripts/check.sh` runs them all (and skips them for docs-only changes):

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run lint-imports
uv run pytest
```

- Branch from `dev` and open your PR against `dev`. Never target `main`; releases are cut from
  it by the maintainer.
- Tests never touch live services: external APIs replay recorded responses from
  `tests/cassettes/`. Don't skip, delete, or loosen tests; update them to match new behavior.
  See [testing](docs/testing.md).
- Never delete, overwrite, or move user music without a safeguard (dry run, trash, or
  confirmation). See [data safety](docs/design/data-safety.md).
- Respect external services: obey rate limits, cache, and send a proper User-Agent. See
  [services etiquette](docs/design/services-etiquette.md).

### Where to read next

- [Documentation index](docs/README.md): scope, principles, design docs
- [Architecture](docs/architecture.md): components and pipeline
- [Glossary](CONTEXT.md): the project's terms (request, candidate, distance, band, ...)
- [Decisions](docs/decisions/): why things are the way they are

## License

[GPL-3.0](LICENSE)
