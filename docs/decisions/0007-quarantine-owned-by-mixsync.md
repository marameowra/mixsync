# ADR 0007: Quarantine lives on /data, owned by MixSync

- **Status:** Accepted
- **Date:** 2026-10-17
- **Note:** the request budget (5 dropped candidates) is planned, not built (#203); the quarantine purge is planned (#208).

## Context
Rejected and review candidates stayed in `/downloads/<peer folder>/`, where slskd put them. [matching](../design/matching.md) promised a quarantine, but none existed, and nothing ever cleaned `/downloads`. `/downloads` belongs to slskd, and slskd's `retention.files.complete` setting deletes files there by age ([slskd config](https://github.com/slskd/slskd/blob/22421286fe2216950a01fa0fb7f2a82836ff9074/docs/config.md)), so files a person still had to review could disappear without a journal entry. soularr has the same problem with its `failed_imports/` folder, which fills the disk ([soularr#137](https://github.com/mrusse/soularr/issues/137)). See [workflow prior art](../research/workflow-prior-art.md).

## Decision
- Quarantine is a MixSync-owned directory under `/data`. Each candidate a request drops (a rejection or a failed transfer, once bytes have landed) moves there as a journaled file op.
- **Review** candidates stay until a person decides. **Rejected** candidates are purged after `MIXSYNC_TRASH_DAYS` by the same journaled purge job as the trash.
- A **request budget** of 5 dropped candidates per request bounds how much quarantine can hold. When the budget is spent, the request goes to `failed`.
- Rejected candidates left in `/downloads` from before this ADR are not moved automatically.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **`/data/quarantine` (chosen)** | Owned and retained by MixSync only; follows the journal and trash rules | `/downloads` and `/data` are separate mounts, so each move is a full copy |
| `/downloads/.quarantine` | Instant rename on the same mount | Depends on unverified slskd retention behaviour for subfolders; slskd still owns the mount |
| Leave files in place, require slskd retention off | No code | Relies on the operator's slskd config; `/downloads` still grows without limit |

## Consequences
- Rejecting a candidate costs one cross-mount copy. The request budget caps this at 5 albums per request.
- Review evidence can no longer vanish because of another service's settings.
- Moving quarantine somewhere else later means migrating the files already in it.
