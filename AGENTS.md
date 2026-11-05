# mixsync

mixsync is a self-hosted music stack: download (Soulseek, torrent, Usenet), verify against
MusicBrainz/AcoustID, organize on disk, and hand off to Navidrome/Plex/Jellyfin.

- Data safety. Never delete, overwrite, or move user music files without a safeguard (dry
  run, backup, trash/quarantine, or confirmation). If a destructive action is the only
  route, stop and report back instead. See [data safety](docs/design/data-safety.md).
- Respect external services. Obey rate limits and terms, cache where sensible, and never
  hammer an API. Limits and User-Agent rules: [services etiquette](docs/design/services-etiquette.md).
- Checks. The full suite is slow, so run the full `scripts/check.sh` (every CI step, skipped
  for docs-only changes) once, locally, before pushing; `/ship` does this. Implementers and
  reviewers run only the tests relevant to the change, plus `scripts/check.sh --fast` (no
  pytest), which the implementer's Stop hook runs.
- Don't weaken tests (anything under `tests/`): no deleting or emptying files and no new
  `skip`/`skipif`/`xfail`/`.only` markers (`.claude/hooks/guard_tests.py` blocks these). When
  behavior changes, rewrite assertions to the new behavior instead of deleting them. If
  one of these is truly needed, ask first and say why. Test public behavior, not internals.
- Shipping: branch from `origin/dev`, then `/ship`. It runs the checks, opens the PR with
  `--base dev` and `Fixes #n` lines, and queues `gh pr merge --auto --merge` only if checks
  passed; otherwise it leaves the PR for the user. The `dev` ruleset requires the `check`
  and `postgres` jobs. Merging to `dev` publishes `:dev`, and a workflow closes the `Fixes`
  issues.
- `main` and releases are human-only: no PRs or merges into `main`, no `v*` tags, GitHub
  releases, or `release` environment approvals. `.claude/hooks/guard_main_merge.py` blocks
  these. A release is the user merging `dev` into `main` with a bumped `version`.

## Delegation
- Hand all implementation (features, fixes, tests, refactors) to the `implementer`
  subagent. The main session plans, writes the brief, reviews, runs the full check once
  before pushing, and opens PRs. It does not write the code itself.
- Run independent pieces of work as parallel implementers. Launch each with
  `isolation: "worktree"` and tell them to `git switch -c <branch> origin/dev` first
  (new worktrees start from `origin/main`).
- Brief each implementer fully: files to read, deliverables, out of scope, and the report
  format.
