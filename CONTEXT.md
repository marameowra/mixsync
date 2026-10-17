# mixsync

A self-hosted music stack that turns a wish for music into verified, organized files in the library and hands them to a media server.

## Language

### Acquisition

**Request**:
A wish for one release group, artist, or track, made by a person, the watchlist, or discovery.
_Avoid_: Order, job, download

**Candidate**:
One peer's folder that might satisfy a request.
_Avoid_: Result, hit, match

**Request budget**:
The number of candidates a request may start downloading and then drop, for a failed transfer or a rejection, before it needs a person.
_Avoid_: Retry limit, attempts

**Quarantine**:
The place mixsync owns where rejected and review candidates wait, apart from the source's own download area.
_Avoid_: Failed imports, downloads

### Matching

**Close runner-up**:
A second-best release whose score is near enough to the best that picking between them is a coin flip, so a person must decide.
_Avoid_: Tie, ambiguous match
