# ADR 0010: Stored secrets are encrypted with an operator-supplied key

- **Status:** Accepted
- **Date:** 2026-10-20

## Context
MixSync stores per-user API tokens (Navidrome, ListenBrainz) and must send them back to those services, so they cannot be hashed. They need encryption at rest, and the key must not live in the database it protects.

## Decision
- Secrets are encrypted with Fernet (`cryptography`). The operator chooses where the key comes from: `MIXSYNC_SECRET_KEY`, or the key file `/config/secret.key`. If both are present, the env var wins, and a different key in the file logs a warning.
- With neither, MixSync generates a key into `/config/secret.key` with mode `600` and logs once to back it up.
- An existing key file readable by anyone but its owner (looser than `600`) stops startup.
- A key that no longer decrypts the stored secrets does not stop startup. The affected `user_links` are marked `needs_relink` and the UI shows a banner. Nothing else depends on the key.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Env var or key file (chosen)** | Fits both Compose secrets and plain volumes | Two sources to explain |
| Env var only | One source | Awkward for installs that keep everything under `/config` |
| Key stored in the database | No operator step | The encryption protects nothing |

## Consequences
- Losing the key costs only the linked-account tokens, which users re-enter.
- Backups of `/config` must include `secret.key` (or the operator must keep the env var).
