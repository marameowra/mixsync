# Contributing

Bug reports and pull requests are welcome. Open an
[issue](https://github.com/marameowra/mixsync/issues) first for anything larger than a small
fix, so we can agree on the approach.

The rules for humans and agents are in [AGENTS.md](AGENTS.md); this page is the path to
following them.

## Development setup

You need Python 3.13, [uv](https://docs.astral.sh/uv/), `git`, and the GitHub CLI (`gh`).

```sh
gh repo clone marameowra/mixsync && cd mixsync
uv sync                              # install dependencies
uv run python -m devtools.preview    # UI preview with fake services at http://127.0.0.1:8090
gh auth login                        # needed for /ship and the issue workflow
```

## Workflow

1. Pick an issue. Ones labelled `ready-for-agent` are fully specified:
   `gh issue list --label ready-for-agent`.
2. Branch from `dev`: `git fetch && git switch -c feat/<n>-<slug> origin/dev`.
3. Read the [glossary](CONTEXT.md) and the [decisions](docs/decisions/) and
   [design docs](docs/design/) for your area. Use the glossary's terms.
4. Write the change test-first.
5. Open the PR against `dev` with one `Fixes #<n>` line per issue. Never target `main`;
   releases are cut from it by the maintainer.

An issue labelled `epic` needs a "Grilled" comment (from a `/grill-with-docs` session) before
anyone works on it or its sub-issues. If it is missing, ask the maintainer.

## Tests and checks

- While you work, run the tests for the code you touched (e.g.
  `uv run pytest tests/unit/library -n auto`) and `scripts/check.sh --fast` (lint, format,
  types, import rules; no pytest).
- Before you push, run the full `scripts/check.sh`. It runs every CI step and skips them for
  docs-only changes. CI also requires the `postgres` job.
- Don't skip, delete, or loosen tests; when behavior changes, update the assertions. Test public
  behavior, not internals.
- Tests never touch live services: external APIs replay recorded responses from
  `tests/cassettes/`.
- A deliberate change to matcher results needs
  `uv run python -m devtools.regression.replay --update`, with the baseline diff committed
  alongside it.

Details: [testing](docs/testing.md).

## Hard rules

- Never delete, overwrite, or move user music without a safeguard (dry run, trash, or
  confirmation). See [data safety](docs/design/data-safety.md).
- Respect external services: obey rate limits, cache, and send a proper User-Agent. See
  [services etiquette](docs/design/services-etiquette.md).
- ADRs are immutable: add a superseded note instead of editing one, and say so in the PR if your
  change contradicts one.

## Using Claude Code

The repo ships its Claude Code setup; one plugin is all you add.

1. Install the skills plugin: `/plugin install mattpocock-skills@claude-plugins-official`
   (if the marketplace is missing: `/plugin marketplace add anthropics/claude-plugins-official`).
2. Start Claude Code in the repo root and trust the folder, so the hooks load.
3. Check: `/plugin` lists `mattpocock-skills`, `/agents` lists `implementer` and `reviewer`.

What the repo provides:

| What | Where | Does |
| --- | --- | --- |
| `guard_tests.py` hook | `.claude/hooks/` | Blocks deleting or emptying tests and new `skip`/`xfail`/`.only` markers |
| `guard_main_merge.py` hook | `.claude/hooks/` | Blocks PRs or merges into `main`, `v*` tags, and releases |
| `implementer` agent | `.claude/agents/` | Implements a scoped task; can't finish while `scripts/check.sh --fast` fails |
| `reviewer` agent | `.claude/agents/` | Read-only review; run it before merge |
| `/ship` | `.claude/commands/ship.md` | Runs the checks, pushes, opens the PR into `dev`, queues auto-merge if they passed |

Skills from the plugin that fit this repo: `tdd` (features and fixes), `diagnosing-bugs`,
`domain-modeling` (glossary and ADRs), `code-review`, and `pr`.

## Where to read next

- [Documentation index](docs/README.md): scope, principles, design docs
- [Architecture](docs/architecture.md): components and pipeline
- [Glossary](CONTEXT.md): the project's terms (request, candidate, distance, band, ...)
- [Decisions](docs/decisions/): why things are the way they are
