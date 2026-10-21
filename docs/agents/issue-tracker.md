# Issue tracker

Issues live in GitHub Issues on `marameowra/mixsync`. Use the `gh` CLI (it infers the repo from the clone).

- Create: `gh issue create --title "..." --body "..."` (heredoc for multi-line bodies)
- Read: `gh issue view <n> --comments`
- List: `gh issue list --state open --json number,title,labels`
- Comment: `gh issue comment <n> --body "..."`
- Label: `gh issue edit <n> --add-label "..."` / `--remove-label "..."`
- Close: `gh issue close <n> --comment "Fixed in #<pr>, merged to dev."` (closing keywords in PRs into `dev` are ignored, see [AGENTS.md](../../AGENTS.md))

Labels are listed in [CLAUDE.md](../../CLAUDE.md#triage-labels). Only issues are triaged, not PRs. Issues and PRs share one number space, so a bare `#42` may be either.
