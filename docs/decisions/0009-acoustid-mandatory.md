# ADR 0009: AcoustID checks every file

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
Existing tags, including MusicBrainz ids, are often wrong or copied from a mislabelled source. Skipping the fingerprint for tagged files would let exactly those files through unverified. Wrong matches are the main complaint about the tool MixSync replaces.

## Decision
- Every file gets a fingerprint and an AcoustID lookup, including files that already carry MusicBrainz tags and files read during migration.
- If AcoustID is unreachable, the job waits and retries. It never imports without the check.
- A high-scoring AcoustID match to a different recording is a hard veto ([matching](../design/matching.md#scoring-and-bands)).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Always check (chosen)** | Tags can never vouch for themselves | A lookup per track (3 req/s), slower migration |
| Skip when tags carry MBIDs | Fast | Trusts the data being verified |

## Consequences
- Migration time is dominated by lookups; the preflight ETA includes all of them ([migration](../design/migration.md)).
- An AcoustID outage stalls imports instead of degrading them.
