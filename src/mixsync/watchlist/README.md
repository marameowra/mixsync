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
- Each follow is checked **at most once a day**. `next_check_at = now + 24 h ± random(0–4 h)`.
- A scheduler job enqueues due follows with `idempotency_key = watchlist:{follow_id}:{date}`.

## Filters (per follow)
- Release types: album / EP / single / compilation / live (default: album + EP)
- `only_after_follow_date` (default on)
- `weekly_cap` (labels default 5; artists unlimited). Hits over the cap are recorded as `skipped_cap` and shown for manual request.

> [!question] Label cap default
> 5 per week proposed. See [watchlist design](../../../docs/design/watchlist.md).

## May import from
`core`, `db`, `match`, `library` (service rule); `MetadataProvider` injected.

## Tests
Fake `MetadataProvider` + fake clock: new release → one request; owned → skipped; cap enforced; the same day twice → one poll.

## Design docs
[Watchlist](../../../docs/design/watchlist.md)
