# Watchlist

Follow artists or record labels and request their new releases automatically.

## Follows
- A follow points to an **artist MBID** or a **label MBID**, and belongs to a user.
- Filters per follow: release types (album / EP / single / compilation / live), "only releases after the follow date" (default on), and minimum date.
- You can create a follow from an artist or label page, or by searching MB.

## Poller
- Runs at most **once per entity per day**, jittered so follows don't all fire at the same time.
- Queries MB for release groups linked to the artist or label that appeared since the last check.
- Each new release group that passes the filters becomes a request owned by the follower, with `source=watchlist`.
- Requests go through the normal pipeline and the follower's matching profile.
- Release groups already owned or already requested are skipped.

## UI
- The follows list shows the last check, the next check, and the releases found.
- A per-follow history lists what was auto-requested and what happened to each.

> [!question] Label follows
> A label can release a lot. Add a per-follow cap on auto-requests per week? Proposed default: 5 per week, with the excess queued for manual approval.
