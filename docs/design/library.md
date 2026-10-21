# Library layout and release canonicalization

## Path templates
- Layout is a per-library template. Default: `{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}`.
- Sanitization strips characters illegal on any common filesystem (Windows, macOS, Linux, SMB), trims trailing dots and spaces, caps segment length. Collisions get a deterministic suffix; never overwrite.
- A template change moves nothing by itself: it produces a dry-run reorganize plan needing confirmation ([data safety](data-safety.md)).
- `{genre}` comes from MusicBrainz genres (`inc=genres`, not folksonomy tags): the release group's, else the release's, else the primary artist's; ranked by votes, ties alphabetical. The `GENRE` tag gets the top 3, the path only the top 1. None at any level renders `Unknown Genre`; import is never blocked.

## Identifiers stored per track
`recording_mbid`, `release_mbid`, `release_group_mbid`, `artist_mbids`, `acoustid_id`, and `status` (`verified` | `unverified`). Written to tags (Picard's MusicBrainz tag names) and stored in the DB.

## Release-group canonicalization
The same album exists as many MB releases (country, format, remaster); tracks fetched at different times would fracture into duplicate folders. So: one **canonical release per release group**, chosen by policy, stored, reused.

**Policy** (configurable, in order): preferred media (Digital Media, then CD); earliest original release date; most tracks; preferred countries (XW, then the user's); official over promo/bootleg.

**Behaviour**
- The first import from a release group picks and stores the canonical release. Later tracks from any release in the group are matched by recording MBID to its track list, then tagged and filed under it.
- A download holding recordings both on and off the canonical release (bonus tracks) is the fuller edition: its release becomes canonical (atomic switch, only if the stored value is unchanged) and all its files file under it. Files already under the old release are not moved; the fragmentation report and a dry-run merge plan handle them.
- If none of a download's recordings are on the canonical release, it files under its own release and the choice is unchanged.
- No duplicate downloads: a request whose recordings are all owned (any release in the group) finishes as `owned` without searching. At import, files whose recording the group already has are skipped and left in `/downloads`.
- Any other canonical change produces a dry-run merge plan.

## Fragmentation report
Lists release groups whose owned tracks span more than one release (including pre-canonicalization imports): current releases, suggested canonical release, files that would be retagged or moved. **Merge** = dry-run preview → confirm → journaled retag and move, undoable within the [trash window](data-safety.md#1-no-hard-deletes-in-the-normal-flow).

## Cover art
Cover Art Archive for the canonical release, falling back to the release group. Saved as `cover.jpg` in the album folder, optionally embedded (per-library setting).
