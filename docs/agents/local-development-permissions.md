# Local development permissions (Codex)

Trusted checkouts use `.codex/config.toml` to select the `mixsync-local-dev` permission profile. It overrides the user's reviewer setting for mixsync only, not `~/.codex/config.toml`. Worktrees inherit it only if their revision contains the file. Start a new chat to load it. Untrusted projects, managed requirements and session/CLI overrides can change the result. Do not combine it with legacy `sandbox_mode`.

## Filesystem
Extends the built-in `:workspace`: workspace files and temp folders are writable; `.git`, `.agents`, `.codex` and `.aws` stay protected. It also allows writes to `~/.cache/uv`. This is a write boundary, not isolation: reads are broad, and the temp-folder grant is not limited to a task's own scratch directory.

## Network
The proxy allows `github.com`, `api.github.com`, `localhost` and `127.0.0.1` (mode `full`, so HTTP writes work for GitHub). The sandbox blocks raw loopback PostgreSQL connections and preview socket binding; those need reviewed escalation, as do dependency downloads from other hosts. Private-network access is off. Apps, MCP servers and web search have separate permissions.

## Review policy
`auto_review.extra_policy` adds to Codex's base policy and covers feature-branch Git operations, the dev PR lifecycle, synthetic previews, SQLite tests, and a disposable PostgreSQL `mixsync_test` database at `127.0.0.1:55435`. Docker needs individual review: a verified task-owned ephemeral `postgres:16` container with no host mounts, privileges or host networking, and no Docker socket grant.

It governs escalations only; it does not intercept commands the sandbox already allows, and it is not a main/release enforcement mechanism. That is the job of the `guard_main_merge.py` hook ([`.codex/hooks.json`](../../.codex/hooks.json)) and [AGENTS.md](../../AGENTS.md). Production, homelab, music, main/release and destructive operations never get standing authorization.

## Verify the loaded default
```sh
codex --strict-config doctor --json
```
Check `config.load`, and that `sandbox.filesystem_paths` names `mixsync-local-dev` and this project's config as the source of the uv-cache rule. A passing config check does not prove sandbox enforcement.

Docs: [permission profiles](https://learn.chatgpt.com/docs/permissions), [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
