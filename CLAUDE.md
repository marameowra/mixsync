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
- Keep test files. Everything under `tests/` counts, fixtures and cassettes included. Don't
  `rm`, `git rm`, `unlink`, `truncate`, or `>`-redirect onto those paths, and don't Write an
  existing one empty; change them with Edit/Write instead. If a whole file looks obsolete,
  stop and ask. Removing a test or assertion, or adding a skip/xfail, is fine when the
  behavior it covers changed, but it triggers a confirmation prompt, so say why in the report.
- Auto-merge. After opening a PR, queue `gh pr merge --auto --merge` only if the reviewer
  agent reported no unfixed findings and every CI step passed locally. Otherwise leave the
  PR for the user to merge. Merging to main publishes the `:dev` image.
- Releases are human-only. Never create or push `v*` tags, create GitHub releases, or approve
  deployments to the `release` environment. Pushing a `v*` tag runs `release.yml`, which
  waits for the user's approval before publishing `:<tag>` and `:latest`.
