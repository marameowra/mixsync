# Watchlist

Follow artists or labels and request their new releases automatically.

## Follows
- A follow targets an artist or label MBID and belongs to a user. Filters: release types (album / EP / single / compilation / live), "only after the follow date" (default on), minimum date.
- Weekly cap: labels default to 5 auto-requests per week, artists unlimited. Overflow is recorded as `skipped_cap` and listed on the follow's page with a one-click request button.

## Poller
MusicBrainz asks clients not to poll ([rules](services-etiquette.md#musicbrainz)), so checks are cheap and rare.
- **One request per check**: a date-range search (artist: release groups by `firstreleasedate`; label: releases by `date`) since the last check, overlapping 7 days for search-index lag. No full browse.
- A release hit carries `primary-type` and `secondary-types`, so label hits get a real type. A partial date counts up to the end of its period (`1995` matches `[1995-12-31 TO *]`), so a year-only release entered late is still found; the follow's date floor and `min_date` compare only to the hit's precision.
- **Adaptive cadence:** released within 12 months → every 3 days; last release 1-2 years ago → weekly; nothing in 2+ years → every 14 days. Each check lands at a random point in its interval.
- An entity followed by several users is checked once and fans out.
- **Check now** runs an immediate, rate-limited check.
- Each new release group passing the filters, and not already owned or requested, becomes a request owned by the follower with `source=watchlist`, using the follower's matching profile.

The follows list shows last check, next check and releases found, with a per-follow history of what was requested and its outcome.
