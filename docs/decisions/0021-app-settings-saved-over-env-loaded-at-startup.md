# ADR 0021: Saved app settings override the environment and load at startup

- **Status:** Accepted
- **Date:** 2026-11-07

## Context
Every app setting came from `MIXSYNC_*` env vars, so changing one meant editing the env file and restarting. Admins should edit them in the UI (#343). Two choices follow: what wins when both the environment and a saved value set a setting, and when a saved change takes effect. Web and worker are separate processes, and most settings are copied into long-lived worker objects at startup (clients, rate-limit buckets, slot counts, the importer).

## Decision
- An app setting's effective value is the saved value, else the environment value, else the default. The environment is read at resolution time and never copied into the database, so editing the env file still works for any setting nobody saved. Reset clears the saved value.
- A saved value wins across restarts even while the env var is set. The settings page warns on fields where that is the case.
- Each process resolves one snapshot at startup and reads nothing live. A saved change takes effect when both web and worker restart. Each process records the settings version it loaded, and the page shows which ones still need a restart.
- Bootstrap settings (`database_url`, `config_dir`, `secret_key`) and mount paths (`data_dir`, `downloads_dir`, `drop_dir`, `import_source_dir`, `backup_dir`) stay env-only.
- Saved secrets are encrypted with the ADR 0010 key. One that no longer decrypts counts as unsaved and is flagged on the page; startup continues.
- The auto-accept confidence cap keeps its live per-use read.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Saved wins over env (chosen)** | UI edits survive restarts | An env var can be silently ignored; mitigated by the warning |
| Env wins over saved | The env file stays authoritative | Every restart undoes UI edits |
| Copy env into the DB at boot | One place to read | Later env edits are ignored forever |
| **Snapshot at startup, restart to apply (chosen)** | Web and worker always agree; small change | A restart after saving |
| Auto-restart on change | No manual step | Processes exit on their own; installs without a restart policy stop. Deferred to a follow-up issue |
| Live reload | No restart | About ten components must look values up instead of copying them; rate limits, slot counts and logging still can't change live |

## Consequences
- Until a restart, the page shows saved values that are not yet in effect; the restart-pending banner says so.
- Settings export/import (#303) should carry saved app settings.
- ADR 0013's AcoustID user key can now also be saved in the UI.
- The deferred auto-restart came in #349: Apply stores a restart request, and processes started before it exit cleanly for their restart policy to restart. `MIXSYNC_SELF_RESTART=false` turns it off for installs without one. Settings still load only at startup.
