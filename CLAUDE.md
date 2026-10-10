# mixsync

mixsync is a self-hosted music stack: download (Soulseek, torrent, Usenet), verify against
MusicBrainz/AcoustID, organize on disk, and hand off to Navidrome/Plex/Jellyfin.

- Data safety. Never delete, overwrite, or move user music files without a safeguard (dry
  run, backup, trash/quarantine, or confirmation). If a destructive action is the only
  route, stop and report back instead.
- Respect external services. Obey rate limits and terms (MusicBrainz: 1 req/s with a proper
  User-Agent). Cache where sensible, and never hammer an API.
- Before reporting work as done, run every CI step (`.github/workflows/ci.yml`), not just
  tests: `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`,
  `uv run lint-imports`, `uv run pytest`.
