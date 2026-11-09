# mixsync

mixsync is a self-hosted music stack: download (Soulseek via slskd; torrent/Usenet planned, #301), verify against
MusicBrainz/AcoustID, organize on disk, and hand off to Navidrome (Plex/Jellyfin planned, #302).

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
- Epics (`epic` label) need a `/grill-with-docs` session before any work on them or their
  sub-issues, recorded as a "Grilled" comment on the epic. Agents can't run that skill: if
  the comment is missing, stop and ask the user to run `/grill-with-docs` on the epic.
  The grilling session swaps the epic's `needs-triage` for `ready-to-implement`. Starting its
  first sub-issue swaps that for `in-progress`.
- Issues. Every change is tracked in a GitHub issue. Find or open one (`gh issue create`)
  before branching, and name the branch `<type>/<issue#>-<slug>`. The PR body needs
  `Fixes #<n>`, or `Refs #<n>` for partial work; `.claude/hooks/require_issue.py` blocks
  `gh pr create` without one.
- Shipping: branch from `origin/dev`, then `/ship`. It runs the checks, opens the PR with
  `--base dev` and `Fixes #n` lines, and queues `gh pr merge --auto --merge` only if checks
  passed; otherwise it leaves the PR for the user. The `dev` ruleset requires the `check`
  and `postgres` jobs. Merging to `dev` publishes `:dev`, and a workflow closes the `Fixes`
  issues.
- `main` and releases are human-only: no PRs or merges into `main`, no `v*` tags, GitHub
  releases, or `release` environment approvals. `.claude/hooks/guard_main_merge.py` blocks
  these. A release is the user merging `dev` into `main` with a bumped `version`.

## Delegation
- Do small or tightly coupled edits (a handful of tool calls, one or two files, or work
  that needs this session's context) directly in the main session.
- Delegate self-contained, well-specified tasks to `implementer` (Sonnet); pass
  `model: opus` for hard or long-horizon ones.
- Run parallel worktree implementers only for independent, sizeable tracks. Launch each
  with `isolation: "worktree"` and tell them to `git switch -c <branch> origin/dev` first
  (new worktrees start from `origin/main`).
- Brief each implementer fully: files to read, deliverables, out of scope, and the report
  format. The `reviewer` pass before merge is the independent check; don't spawn extra
  agents to re-verify.
- The main session runs the full check once before pushing, and opens PRs.
