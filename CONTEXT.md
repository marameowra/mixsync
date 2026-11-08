# mixsync

A self-hosted music stack that turns a wish for music into verified, organized files in the library and hands them to a media server.

## Language

One line per term. _Avoid_ lists synonyms not to use. Specs: [`docs/design/`](docs/design/).

### Acquisition

**Request**: A wish for one release group, artist, or track, made by a person, the watchlist, or discovery. _Avoid_: Order, job, download

**Candidate**: One peer's folder, or one indexer release (a torrent or NZB), that might satisfy a request. _Avoid_: Result, hit, match

**Download source**: A backend that finds and fetches candidates: Soulseek, or an indexer search paired with its download client. _Avoid_: Source (that is who made the request), backend, client

**Source priority**: The admin's ordered list of download sources. A search tries each in turn and stops at the first that yields an acceptable candidate. _Avoid_: Fallback order

**Origin**: The folder and files a request made from files already on disk (migration, drop folder, adopt) started from. Set once; a request found by searching has none. _Avoid_: Source (that is who made the request), first candidate

**Content key**: What identifies an album found in a drop folder across scans: the drop source plus a hash of its files. An album is taken again only while its requests are under the attempt cap and the latest failed; deleting a request forgets that attempt. _Avoid_: Idempotency key, digest

**Tried list**: The candidates a request has already dropped; the retry searches afresh and skips them. Only the best candidate is downloaded, so no fallback candidates are stored.

**Request budget**: The number of candidates a request may start downloading and then drop before it needs a person. _Avoid_: Retry limit, attempts

**Quarantine**: The place mixsync owns (`/data/quarantine`) where rejected and review candidates wait, apart from slskd's `/downloads`. _Avoid_: Failed imports, downloads

**Release (a download)**: The journaled hard delete of a downloaded file (Soulseek or Usenet) after its library copy is hash-verified; the one allowed hard delete. A seeding download is never released. _Avoid_: Delete, clean up

**Seeding download**: A torrent's files while its torrent client still shares them. mixsync only copies from it, never moves, deletes or releases it, and leaves its removal to the client's own ratio rules. _Avoid_: Torrent payload

### Matching

**Distance**: The weighted sum of per-feature penalties between a file or candidate and the MusicBrainz target; 0 is perfect. _Avoid_: Confidence

**Band**: Where a distance falls against the profile's thresholds: auto-accept, review, or reject.

**Profile**: A user's thresholds: a preset (strict, balanced, loose) or custom, with the auto-accept limit editable as a confidence percentage and held to the admin's app-wide minimum. _Avoid_: Mode, level

**Hard veto**: A signal (e.g. AcoustID names a different recording) that forces review regardless of distance.

**Transcode**: A file in a lossless format whose audio was once lossy, so the quality its format promises is not there. Suspected, never proven: it is a hard veto, not a rejection. _Avoid_: Fake FLAC, upscale

**Padded preview**: A short clip (often 30 s) filled out with silence to look like the full track, so its length still matches. Suspected from trailing silence: a hard veto, not a rejection. _Avoid_: Snippet, sample

**Close runner-up**: A second-best release whose distance is near enough to the best that picking between them is a coin flip, so a person must decide. _Avoid_: Tie, ambiguous match, runner-up (alone)

### Review

**Decision**: The logged verdict on one candidate's match: the band it fell in, its evidence, and, once acted on, who acted, how and when. Kept as the audit record after its request is gone. _Avoid_: Match, result

**Review**: A person acting on a decision that fell in the review band, after its files are downloaded. Each action settles the decision and moves its request on in one step, except a split. _Avoid_: Approval, moderation

**Repick**: A review action naming a different release; the files are verified again against it.

**Split**: A review action that takes files the decision's release has no track for and makes them a new request, made from those files, for a release that holds them. The decision stays pending for the rest of its files. _Avoid_: Partial accept, multi-release match

**Recheck**: A review action that closes a pending decision so the files are verified again with the current scorer.

**Cancelled (decision)**: A pending decision closed because its request was deleted, not because anyone judged the candidate. _Avoid_: Rejected

### Library and safety

**Library**: The organized tree under `/data/library` that the media server reads.

**Album (in the library)**: Every verified library track filed under one release group, whichever edition it came from. An unverified track belongs to the album named by its album artist, album and year tags, even when it carries a release group, so one record can show as a verified album and an unverified one. _Avoid_: Release (that is one edition), folder

**Album artist**: The artist credited for a whole album (the `albumartist` tag), which a compilation gives as "Various Artists". It names and groups albums; the artist of each track can differ. _Avoid_: Artist (when the album credit is meant)

**Trash**: `/data/.trash/<day>/`, where "delete" moves files; purged by a journaled job after `MIXSYNC_TRASH_DAYS`. _Avoid_: Recycle bin

**Journal**: The record of every file operation, which makes imports and undo recoverable after a crash.

**Plan**: A dry-run list of moves or retags for a bulk operation (merge, reorganize, promote), confirmed before it runs and undoable for a limited window. _Avoid_: Batch

**Unverified**: An item imported without a confident MusicBrainz match, tagged `MIXSYNC_STATUS=unverified`.

**Suggestion**: Album and track details from a service other than MusicBrainz (Deezer, Discogs), fetched only when a reviewer asks, to pre-fill Mark unverified. The reviewer still edits and submits; a suggestion never affects matching or makes an item verified. _Avoid_: Fallback match, secondary provider

### Watchlist and discovery

**Follow**: A user's subscription to an artist or label MBID whose new releases become requests.

**Discovery**: Recommendation-driven requests (`source=discovery`) from ListenBrainz.

**Provisional**: A discovery track imported into `/data/discover` on trial until kept or trashed. _Avoid_: Temporary

### Playlists

**Discovery playlist**: A per-user playlist generated from listening history; its unowned tracks are provisional.

**Mirrored playlist**: A per-user copy of a playlist curated elsewhere, keeping its upstream order and membership. Its missing tracks become ordinary requests (`source=playlist`), never provisional; a track leaving upstream leaves the playlist only. _Avoid_: Synced, imported, external playlist

**Smart playlist**: A playlist built from rules over the owned library only; it never makes requests. _Avoid_: Dynamic, auto playlist

**Unresolved track**: A playlist entry that could not be matched confidently to a recording; listed on the playlist, never guessed. _Avoid_: Missing track (that is a resolved recording not yet owned)

### Administration

**App setting**: A deployment-wide value an admin edits in the UI; once saved it overrides the environment and holds across restarts. Distinct from a user's Profile. _Avoid_: Config, preference, option

**Setting source**: Where an app setting's effective value comes from: saved (by an admin), environment (`MIXSYNC_*`), or default. _Avoid_: Origin, override
