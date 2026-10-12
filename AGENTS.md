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
- Don't weaken tests. A hook prompts on each of these, so avoid them; everything under
  `tests/` counts, fixtures and cassettes included:
  - Deleting or emptying a file: no `rm`, `git rm`, `unlink`, `truncate`, `>` redirects, or
    an empty Write. Change test files with Edit/Write. If a file looks obsolete, ask.
  - Fewer assertions: when behavior changes, rewrite a test's assertions to the new
    behavior instead of deleting them or the test.
  - Skip markers: no `skip`, `skipif`, `xfail`, or `.only`. Fix the code under test.
  If one of these is truly needed, ask first and say why. Write tests against public
  behavior, not internals, so refactors don't force rewriting them.
- Auto-merge. After opening a PR, queue `gh pr merge --auto --merge` only if the reviewer
  agent reported no unfixed findings and every CI step passed locally. Otherwise leave the
  PR for the user to merge. Merging to main publishes the `:dev` image.
- Releases are human-only. Never create or push `v*` tags, create GitHub releases, or approve
  deployments to the `release` environment. Pushing a `v*` tag runs `release.yml`, which
  waits for the user's approval before publishing `:<tag>` and `:latest`.

## Delegation
- Hand all implementation (features, fixes, tests, refactors) to the `implementer`
  subagent. The main session plans, writes the brief, reviews, re-runs checks,
  and opens PRs. It does not write the code itself.
- Run independent pieces of work as parallel implementers. Launch each with
  `isolation: "worktree"`, branched from the pushed base branch, so they never
  share a checkout.
- Brief each implementer like the earlier slices: files to read, deliverables,
  out of scope, the full check list, and the report format.
