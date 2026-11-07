# ADR 0013: One AcoustID user key per deployment

- **Status:** Accepted
- **Date:** 2026-10-10

## Context
After a promote plan, MixSync submits fingerprints to AcoustID, which needs a user key. A per-user key would mean another linked account, more stored secrets and more UI.

## Decision
- One key, `MIXSYNC_ACOUSTID_USER_KEY`, is set in the environment. All submissions go under that account.
- No `user_links` row exists for AcoustID.
- Undoing a promote does not retract submissions: AcoustID has no retract.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Deployment-wide key (chosen)** | No per-user storage or UI | Submissions are not attributed to individual users |
| Per-user key in `user_links` | Attribution | More secrets to store and relink; little benefit at household scale |

## Consequences
- Lookups use a separate app key; see [services etiquette](../design/services-etiquette.md).
