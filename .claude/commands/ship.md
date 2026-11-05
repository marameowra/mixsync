---
name: "Ship"
description: "Check, push, open a PR into dev, and queue auto-merge"
allowed-tools: Bash(git:*), Bash(gh:*), Bash(scripts/check.sh:*)
category: "Workflow"
tags: ["workflow", "git"]
---

Ship the current branch as a PR into `dev`.

1. Run `git branch --show-current`. If it is `main` or `dev`, stop and refuse.
2. Run `scripts/check.sh`. Record whether it passed.
3. Run `git push -u origin HEAD`.
4. Find the issues this work addresses from the branch name and commits. If unknown, ask the user for the numbers. Run `gh pr create --base dev` with a title and a body that has a short summary, one `Fixes #<n>` line per issue, and ends with:
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
5. Only if step 2 passed (docs-only skips count as passed), run `gh pr merge --auto --merge`. Otherwise leave the PR open and tell the user which check failed.

Never target `main` and never merge into it.
