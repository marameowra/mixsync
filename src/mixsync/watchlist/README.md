# `watchlist`

## Purpose
Follow artists and labels; turn their new release groups into requests automatically.

## Owns / does not own
- **Owns:** follow CRUD, the poll schedule, filters, weekly caps, `follow_hits` bookkeeping.
- **Does not own:** querying MusicBrainz (injected `MetadataProvider`), or downloading (normal request pipeline).

## Proposed files
| File | Contents |
|---|---|
| `follows.py` | Create/update/remove follows; filter validation |
| `poller.py` | Job handler `watchlist.poll(follow_id)`: browse release groups since `last_checked_at` → filter → skip owned/requested → apply cap → create requests → schedule the next check |

## Schedule
- **Adaptive interval** per followed entity: 3 days if it released something in the last 12 months, weekly if the last release was 1–2 years ago, 14 days if older ([watchlist design](../../../docs/design/watchlist.md#poller)).
- `next_check_at = now + uniform(0.5 × interval, 1.5 × interval)`, so checks land at random points and never at a fixed clock time.
- One check per **entity**, not per follow: followers of the same artist share it (`idempotency_key = watchlist:{entity}:{mbid}:{window_start}`).
- Each check is **one MB search request** with a date range and a 7-day overlap (see [metadata](../metadata/README.md#rules)).
- **Check now** enqueues an immediate check.

## Filters (per follow)
- Release types: album / EP / single / compilation / live (default: album + EP)
- `only_after_follow_date` (default on)
- `weekly_cap` (labels default 5; artists unlimited). Hits over the cap are recorded as `skipped_cap` and shown for manual request.

**Decided:** labels default to 5 per week; artists default to unlimited. See [watchlist design](../../../docs/design/watchlist.md).

## May import from
`core`, `db`, `match`, `library` (service rule); `MetadataProvider` injected.

## Tests
Fake `MetadataProvider` + fake clock: new release → one request; owned → skipped; cap enforced; the same day twice → one poll.

## Design docs
[Watchlist](../../../docs/design/watchlist.md)
