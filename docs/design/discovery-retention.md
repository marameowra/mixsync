# Discovery and retention

Build playlists from each user's listening, including tracks they don't own yet, and give unowned tracks a trial period.

Prior art to study: **Explo**, which downloads ListenBrainz weekly recommendations into Navidrome and cleans them up afterwards.

## Inputs
- Navidrome scrobbles to ListenBrainz and/or Last.fm. MixSync doesn't need to collect plays itself.
- Each user links ListenBrainz (username + token) and/or Last.fm (username + API key).
- Navidrome plays and stars are read through the Subsonic API, per linked Navidrome account.

## Generator
- Sources: ListenBrainz personal recommendations and LB Radio prompts, plus Last.fm similar artists/tracks seeded by the user's top artists.
- A playlist definition holds: name, sources, size (default 50), share of unowned tracks (default 30%), refresh cadence (default weekly).
- Owned tracks are resolved by recording MBID. Unowned tracks become requests with `source=discovery` and `provisional=true`.
- Playlists are pushed to Navidrome per user through Subsonic `createPlaylist` / `updatePlaylist`.

## Provisional library
- Unowned discovery tracks are imported into **`/data/discover`**, which is a second Navidrome library.
- They go through the same matching and import pipeline.

## Retention
- Each provisional track has an `expires_at` (default import date + 30 days, configurable per user).
- **Keep** if any of these is true before expiry:
  - the track is starred in Navidrome by its user
  - it has at least N plays (default 3)
  - the user clicks Keep in MixSync
- Keeping a track **promotes** it: it is re-imported into the main library through the normal importer, with canonicalization.
- At expiry the track is moved to `.trash` ([data safety](data-safety.md)), never deleted outright.
- A provisional track shared by several users' playlists expires only when no user wants to keep it.
- A digest page lists what expires in the next 7 days.

**Defaults (decided):** 30-day retention, keep after 3 plays, weekly refresh, 50 tracks with 30% unowned. All of them can be changed per user or per playlist.
