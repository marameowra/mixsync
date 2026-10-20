# `acquire`

## Purpose
The request pipeline and everything that turns files into library tracks: search, download, verify, import or review, plus merge, migration, drop folder and re-check.

## Owns / does not own
- **Owns:** the job handlers (`pipeline.py`), the Request lifecycle (`lifecycle.py`, `requests.py`), VERIFY jobs (`verify.py`), the stored decision evidence (`evidence.py`), the download cap (`gate.py`), merge and undo (`merge.py`), library migration (`migration.py`), the drop folder and adopter (`dropfolder.py`, `adopter.py`), re-check and promote (`recheck.py`), AcoustID submission (`acoustid_submit.py`).
- **Does not own:** scoring (`match`), the file operations themselves (`library`), or any HTTP. Adapters are injected as `core` Protocols.

## Rules / invariants
- Every Request status change goes through `lifecycle.py`; transitions only set fields, and the caller's job transaction commits them.
- A handler does its I/O outside any transaction, then writes its result and enqueues the next job in one transaction. The next job's idempotency key derives from the current job id, so a re-run after a crash never forks the chain.
- VERIFY payloads and `request-<id>-decision-<id>` batch ids are built and read only in `verify.py`. Decision evidence JSON is read only through `evidence.py`.
- Merge, promote and migration act through plans: a draft (dry run) first, then a user confirmation. Every op is validated against the DB and the file journal before it acts, and a failure marks the plan failed instead of guessing ([data safety](../../../docs/design/data-safety.md)).
- The migration source is only ever opened for reading.
- The request budget of 5 dropped candidates is in [ADR 0007](../../../docs/decisions/0007-quarantine-owned-by-mixsync.md).
  > Planned — not built (#203): the 5-candidate request budget.

## May import
`core`, `db`, `match`, `library`.

## Design doc
[Matching](../../../docs/design/matching.md) · [Migration](../../../docs/design/migration.md) · [Unverified imports](../../../docs/design/unverified-and-mb-submit.md) · [ADR 0004](../../../docs/decisions/0004-sqlite-and-db-job-queue.md)
