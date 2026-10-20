# Discovery and retention

Build playlists from each user's listening, including tracks they don't own yet, and give unowned tracks a trial period. Prior art: Explo.

## Inputs
- Navidrome scrobbles to ListenBrainz; MixSync collects no plays itself. Each user links ListenBrainz (username + token).
- ListenBrainz is the only discovery source (Last.fm is [deferred](services-etiquette.md#lastfm)). Navidrome plays and stars are read through Subsonic per linked account.

## Generator
- Sources: ListenBrainz personal recommendations and LB Radio prompts (artist prompts cover similar-artist expansion).
- A playlist definition holds name, sources, size (default 50), share of unowned tracks (default 30%) and refresh cadence (default weekly), all editable per user or playlist.
- Owned tracks resolve by recording MBID. Unowned ones become requests with `source=discovery` and `provisional=true`, imported through the normal pipeline into `/data/discover` (Navidrome's second library).
- Playlists are pushed per user via Subsonic `createPlaylist` / `updatePlaylist`.

## Retention
- Each provisional track has an `expires_at` (default 30 days, per user). A hold's clock starts when the track joins that user's playlist, or at import for the hold that triggered the download.
- **Keep** before expiry if the user stars it in Navidrome, it has at least 3 plays, or the user clicks Keep. Keeping **promotes**: the normal importer files it in the main library (canonical release honoured) and the discover copy is trashed.
- At expiry the track moves to `.trash` ([data safety](data-safety.md)), never deleted outright.
- A track in several users' playlists expires only when no user wants it.
- A digest page lists what expires in the next 7 days.
