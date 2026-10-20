# mixsync

A self-hosted music stack that turns a wish for music into verified, organized files in the library and hands them to a media server.

## Language

One line per term. _Avoid_ lists synonyms not to use. Specs: [`docs/design/`](docs/design/).

### Acquisition

**Request**: A wish for one release group, artist, or track, made by a person, the watchlist, or discovery. _Avoid_: Order, job, download

**Candidate**: One peer's folder that might satisfy a request. _Avoid_: Result, hit, match

**Tried list**: The candidates a request has already dropped; the retry searches afresh and skips them. Only the best candidate is downloaded, so no fallback candidates are stored.

**Request budget**: The number of candidates a request may start downloading and then drop before it needs a person. _Avoid_: Retry limit, attempts

> Planned, not built (#203): the 5-candidate cap in [ADR 0007](docs/decisions/0007-quarantine-owned-by-mixsync.md) is not in code yet.

**Quarantine**: The place mixsync owns (`/data/quarantine`) where rejected and review candidates wait, apart from slskd's `/downloads`. _Avoid_: Failed imports, downloads

**Release (a download)**: The journaled hard delete of a source file from `/downloads` after its library copy is hash-verified; the one allowed hard delete. _Avoid_: Delete, clean up

### Matching

**Distance**: The weighted sum of per-feature penalties between a file or candidate and the MusicBrainz target; 0 is perfect. _Avoid_: Confidence

**Band**: Where a distance falls against the profile's thresholds: auto-accept, review, or reject.

**Profile**: A user's preset thresholds (strict, balanced, loose), editable. _Avoid_: Mode, level

**Hard veto**: A signal (e.g. AcoustID names a different recording) that forces review regardless of distance.

**Close runner-up**: A second-best release whose distance is near enough to the best that picking between them is a coin flip, so a person must decide. _Avoid_: Tie, ambiguous match, runner-up (alone)

### Library and safety

**Library**: The organized tree under `/data/library` that the media server reads.

**Trash**: `/data/.trash/<day>/`, where "delete" moves files; purged by a journaled job after `MIXSYNC_TRASH_DAYS`. _Avoid_: Recycle bin

**Journal**: The record of every file operation, which makes imports and undo recoverable after a crash.

**Plan**: A dry-run list of moves or retags for a bulk operation (merge, reorganize, promote), confirmed before it runs and undoable for a limited window. _Avoid_: Batch

**Unverified**: An item imported without a confident MusicBrainz match, tagged `MIXSYNC_STATUS=unverified`.

### Watchlist and discovery

**Follow**: A user's subscription to an artist or label MBID whose new releases become requests.

**Discovery**: Recommendation-driven requests (`source=discovery`) from ListenBrainz.

**Provisional**: A discovery track imported into `/data/discover` on trial until kept or trashed. _Avoid_: Temporary
