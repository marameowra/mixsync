# Local development permissions

Trusted MixSync checkouts use `.codex/config.toml` to select the
`mixsync-local-dev` permission profile. The project config overrides the user's
reviewer setting for MixSync only; it does not change `~/.codex/config.toml`.
New worktrees inherit the default when their checked-out revision contains this
file. Existing worktrees on older revisions need the file copied explicitly or
must merge a revision containing it. Start a new chat to load the project default;
active chats can retain their existing permission settings. Untrusted projects,
managed requirements and explicit session/CLI overrides can change the result.
Do not combine this profile with legacy `sandbox_mode` settings.

The profile extends Codex's built-in `:workspace`: workspace files and temporary
folders are writable; `.git`, `.agents`, `.codex` and `.aws` remain protected.
It additionally permits writes to `~/.cache/uv`. It does not add the main checkout
as a writable root when another worktree is active. The inherited read access
is broad: this is a write boundary, not isolation from readable personal files.
The built-in temporary-folder write grant also applies outside this task's own
scratch directory; task ownership is an instruction, not a filesystem boundary.

The enabled network proxy permits `github.com`, `api.github.com`, `localhost`
and `127.0.0.1`. Network mode `full` permits HTTP write methods required by GitHub
operations while retaining the domain allowlist. Hosts are not limited to one
repository, protocol or port. Direct raw loopback PostgreSQL connections and
preview socket binding remain blocked by the tested macOS sandbox and need
individual reviewed escalations. Broad private-network access stays disabled.
Dependency downloads outside these hosts also need reviewed escalation. Apps,
MCP servers and web search have separate permissions.

`auto_review.extra_policy` contains additional Markdown instructions alongside
Codex's base review policy. It describes ordinary feature Git operations, the
dev PR lifecycle, synthetic previews, SQLite tests, and a disposable PostgreSQL
`mixsync_test` database at `127.0.0.1:55435`. Docker commands require individual
review and a verified task-owned ephemeral `postgres:16` container with no host
mounts, privileges or host networking. There is no Docker socket grant.

Review instructions govern escalations; they do not intercept every command
already allowed by the sandbox. The profile does not install a Git hook or a
complete main/release enforcement mechanism. Keep existing hooks and repository
instructions. Sandbox escalation can exceed the profile's filesystem and
network boundaries, so review each exact command. Production, homelab, music,
main/release and destructive operations do not acquire standing authorization.

## Verify the loaded default

Run from the checkout with the installed Codex CLI:

```sh
codex --strict-config doctor --json
```

Check `config.load` and `sandbox.filesystem_paths`: the latter should identify
`mixsync-local-dev` and this project's config as the source of the uv-cache rule.
Doctor's overall status includes unrelated connectivity and installation checks;
a successful config check alone does not prove sandbox enforcement.

The config was parsed by CLI 0.163.0 and Desktop CLI 0.163.0-alpha.10.2. On macOS,
`codex sandbox -P mixsync-local-dev` allowed a GitHub API GET and rejected a
non-allowlisted example.com request with HTTP 403. TCP connection to the loopback
test PostgreSQL port and an ephemeral loopback bind were rejected without SQL
or persistent changes. The PostgreSQL suite was not run against another active
chat's database.

References: [permission profiles](https://learn.chatgpt.com/docs/permissions),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
