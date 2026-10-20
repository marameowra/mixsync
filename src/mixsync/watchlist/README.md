# `watchlist`

## Purpose
Follow artists and labels; turn their new release groups into requests automatically.

## Owns / does not own
- **Owns:** follow CRUD and filter validation (`follows.py`), the poll schedule and job handler (`poller.py`), `follow_hits` bookkeeping.
- **Does not own:** querying MusicBrainz (injected `MetadataProvider`) or downloading (the normal request pipeline).

## Rules / invariants
- One check per **entity**, not per follow: followers of the same artist share it (`idempotency_key = watchlist:{entity}:{mbid}:{window_start}`).
- Each check is one MusicBrainz search with a date range since `last_checked_at` minus a 7-day overlap ([metadata](../metadata/README.md)).
- `next_check_at` is randomized within 0.5x to 1.5x of an adaptive interval, so checks never land at a fixed clock time. Intervals: [watchlist design](../../../docs/design/watchlist.md#poller).
- Only releases dated after the follow (less the overlap) are auto-requested; the back catalogue is picked by hand.
- Over-cap hits are recorded as `skipped_cap`, never dropped. Defaults: [ADR 0015](../../../docs/decisions/0015-watchlist-weekly-cap-defaults.md).

## May import
`core`, `db`, `match`, `library`, `acquire`; `MetadataProvider` is injected.

## Design doc
[Watchlist](../../../docs/design/watchlist.md)
