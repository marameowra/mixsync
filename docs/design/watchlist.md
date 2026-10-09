# Watchlist

Follow artists or record labels and request their new releases automatically.

## Follows
- A follow points to an **artist MBID** or a **label MBID**, and belongs to a user.
- Filters per follow: release types (album / EP / single / compilation / live), "only releases after the follow date" (default on), and minimum date.
- You can create a follow from an artist or label page, or by searching MB.

## Poller
MusicBrainz asks clients not to poll for changes ([rules](services-etiquette.md#musicbrainz)), so a check is kept as cheap and as rare as is still useful:
- **One request per check:** a date-range search (artist: release groups with `firstreleasedate` since the last check; label: releases with `date` since the last check). The window overlaps by 7 days to cover search-index lag. There's no full browse.
- **Adaptive cadence** per followed entity:

| Entity activity | Interval |
|---|---|
| Released something in the last 12 months | every 3 days |
| Last release 1–2 years ago | weekly |
| Nothing in 2+ years | every 14 days |

- Each check is scheduled at a **random point in its interval**, never at a fixed clock time.
- An entity followed by several users is checked once, and the results fan out to each follower.
- A **Check now** button on a follow runs an immediate check (rate limited like everything else).
- Each new release group that passes the filters becomes a request owned by the follower, with `source=watchlist`.
- Requests go through the normal pipeline and the follower's matching profile.
- Release groups already owned or already requested are skipped.

## UI
- The follows list shows the last check, the next check, and the releases found.
- A per-follow history lists what was auto-requested and what happened to each.

**Weekly cap (decided):** each follow has a cap on auto-requests per week. Labels default to **5 per week**; artists default to unlimited. Releases over the cap are recorded as `skipped_cap` and listed on the follow's page with a one-click request button.
