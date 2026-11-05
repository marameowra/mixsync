# Issue tracker

Issues live in GitHub Issues on `marameowra/mixsync`. Use the `gh` CLI (it infers the repo from the clone).

- Create: `gh issue create --title "..." --body "..."` (heredoc for multi-line bodies)
- Read: `gh issue view <n> --comments`
- List: `gh issue list --state open --json number,title,labels`
- Comment: `gh issue comment <n> --body "..."`
- Label: `gh issue edit <n> --add-label "..."` / `--remove-label "..."`
- Close: put `Fixes #<n>` in the PR body. GitHub ignores closing keywords in PRs into `dev`, so [`close-issues-on-dev.yml`](../../.github/workflows/close-issues-on-dev.yml) closes them on merge. To close one by hand: `gh issue close <n> --comment "Fixed in #<pr>, merged to dev."`

Labels are listed in [CLAUDE.md](../../CLAUDE.md#triage-labels). Only issues are triaged, not PRs. Issues and PRs share one number space, so a bare `#42` may be either.
