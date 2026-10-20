# mixsync

A self-hosted music stack that turns a wish for music into verified, organized files in the library and hands them to a media server.

## Language

One line per term. _Avoid_ lists synonyms not to use. Specs: [`docs/design/`](docs/design/).

### Acquisition

**Request**: A wish for one release group, artist, or track, made by a person, the watchlist, or discovery. _Avoid_: Order, job, download

**Candidate**: One peer's folder that might satisfy a request. _Avoid_: Result, hit, match

**Origin**: The folder and files a request made from files already on disk (migration, drop folder, adopt) started from. Set once; a request found by searching has none. _Avoid_: Source (that is who made the request), first candidate

**Content key**: What identifies an album found in a drop folder across scans: the drop source plus a hash of its files. An album is taken again only while its requests are under the attempt cap and the latest failed; deleting a request forgets that attempt. _Avoid_: Idempotency key, digest

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

**Transcode**: A file in a lossless format whose audio was once lossy, so the quality its format promises is not there. Suspected, never proven: it is a hard veto, not a rejection. _Avoid_: Fake FLAC, upscale

**Padded preview**: A short clip (often 30 s) filled out with silence to look like the full track, so its length still matches. Suspected from trailing silence: a hard veto, not a rejection. _Avoid_: Snippet, sample

**Close runner-up**: A second-best release whose distance is near enough to the best that picking between them is a coin flip, so a person must decide. _Avoid_: Tie, ambiguous match, runner-up (alone)

### Review

**Decision**: The logged verdict on one candidate's match: the band it fell in, its evidence, and, once acted on, who acted, how and when. Kept as the audit record after its request is gone. _Avoid_: Match, result

**Review**: A person acting on a decision that fell in the review band, after its files are downloaded. Each action settles the decision and moves its request on in one step. _Avoid_: Approval, moderation

**Repick**: A review action naming a different release; the files are verified again against it.

**Recheck**: A review action that closes a pending decision so the files are verified again with the current scorer.

**Cancelled (decision)**: A pending decision closed because its request was deleted, not because anyone judged the candidate. _Avoid_: Rejected

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
