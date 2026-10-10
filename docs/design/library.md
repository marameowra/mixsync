# Library layout and release canonicalization

## Path templates
- The on-disk layout is a template built from metadata, configurable per library.
- Default: `{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}`
- Alternatives can be selected, e.g. `{genre}/{albumartist}/{album}/...`
- Sanitization strips characters that are illegal on any common filesystem (Windows, macOS, Linux, SMB shares), trims trailing dots and spaces, and caps segment length.
- Collisions get a deterministic suffix and are never overwritten.
- A template change does **not** move files automatically. It produces a dry-run reorganize plan, which needs confirmation ([data safety](data-safety.md)).

**Decided:** the default template above stays.

**Genre source: MusicBrainz genres** (the curated genre list, not free-form folksonomy tags), requested with `inc=genres`:
1. Use the release group's genres. If it has none, fall back to the release's genres, then the primary artist's.
2. Rank by vote count; break ties alphabetically.
3. The `GENRE` tag gets the top 3. The `{genre}` path field uses only the top 1, because a track can only live in one folder.
4. If there are no genres at any level, `{genre}` renders as `Unknown Genre`. The file is never blocked from importing.

## Identifiers stored per track
- `recording_mbid`, `release_mbid`, `release_group_mbid`, `artist_mbids`, `acoustid_id`
- `status`: `verified` | `unverified`
- These are written to tags (standard MusicBrainz tag names, matching Picard's) and stored in the DB.

## Release-group canonicalization
**Problem:** the same album exists as many MB releases (country, format, remaster). Tracks downloaded at different times attach to different releases, so the library fractures into duplicate album folders.

**Solution:** one **canonical release per release group**, chosen by policy, stored, and reused.

### Canonical release policy (configurable, evaluated in order)
1. Preferred media (default: Digital Media, then CD)
2. Original release date (earliest)
3. Most tracks (prefers deluxe editions when the user allows them)
4. Preferred countries (default: XW, then the user's country)
5. Official status over promo or bootleg

### Behaviour
- When the first track from a release group is imported, the policy picks the canonical release and stores it.
- Later tracks from **any release in that group** are matched by recording MBID to the canonical release's track list, then tagged and filed under the canonical release.
- If a download holds recordings that are on the canonical release **and** recordings that are not (bonus tracks), its own release is the fuller edition and **becomes the canonical release**: the stored choice is switched atomically (only if it still holds the old value), and all of its files are filed under it. Files already under the old canonical release are not moved; they show up in the fragmentation report and are handled by a dry-run merge plan.
- If none of a download's recordings are on the canonical release, it is filed under its own release and the canonical choice is unchanged.
- **No duplicate downloads:** a request whose recordings are all already in the library (any release in the group) is finished as `owned` without searching. At import time, files whose recording the group already has are skipped and left in `/downloads` (nothing is deleted without a verified import).
- Changing a release group's canonical release after the fact (other than the bonus-track switch above) produces a dry-run merge plan.

## Fragmentation report
- Lists release groups whose owned tracks span more than one release, including anything imported before canonicalization existed.
- Per group: the current releases, the proposed canonical release, and the files that would be retagged or moved.
- **Merge** = dry-run preview → confirm → journaled retag and move (undoable).

## Cover art
- Fetched from the Cover Art Archive for the canonical release, falling back to the release group.
- Saved as `cover.jpg` in the album folder and optionally embedded (a per-library setting).
