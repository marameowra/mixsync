# Issue tracker

Issues live in GitHub Issues on `marameowra/mixsync`. Use the `gh` CLI (it infers the repo from the clone).

- Create: `gh issue create --title "..." --body "..."` (heredoc for multi-line bodies)
- Read: `gh issue view <n> --comments`
- List: `gh issue list --state open --json number,title,labels`
- Comment: `gh issue comment <n> --body "..."`
- Label: `gh issue edit <n> --add-label "..."` / `--remove-label "..."`
- Close: put `Fixes #<n>` in the PR body. GitHub ignores closing keywords in PRs into `dev`, so [`close-issues-on-dev.yml`](../../.github/workflows/close-issues-on-dev.yml) closes them on merge. To close one by hand: `gh issue close <n> --comment "Fixed in #<pr>, merged to dev."`

Labels are listed in [CLAUDE.md](../../CLAUDE.md#triage-labels). Only issues are triaged, not PRs. Issues and PRs share one number space, so a bare `#42` may be either.

## Priority, milestones, and epics

- **Priority:** every open issue carries one of `priority: high`, `priority: medium`, `priority: low`. Sub-issues inherit their parent's priority unless labelled otherwise.
- **Milestones are releases** (e.g. `v1.1`): the set of issues meant to ship in the next `dev` → `main` merge. Don't use a milestone as a backlog bucket; `priority: low` already says "not now". The maintainer closes a milestone when the release ships.
- **Epics** are parent issues labelled `epic`, split into sub-issues (`gh api graphql` `addSubIssue`, or the "Create sub-issue" button). Split an epic before work starts. Only sub-issues get `ready-for-agent`; an epic never does, so each agent task stays one PR.
- **Epic status labels:**
  - `needs-triage` until grilled.
  - Then `ready-to-implement` once the "Grilled" comment is posted and the sub-issues are filed.
  - Then `in-progress` from when work on the first sub-issue starts: `gh issue edit <epic> --remove-label ready-to-implement --add-label in-progress`.
  - An epic carries only one of these at a time, and never `ready-for-human`.
- **Closing an epic:** PRs reference sub-issues (`Fixes #<sub-issue>`). GitHub doesn't close a parent when its last sub-issue closes, so close the epic by hand.
