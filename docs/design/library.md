# Library layout and release canonicalization

## Path templates
- The on-disk layout is a template built from metadata, configurable per library.
- Default: `{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}`
- Alternatives can be selected, e.g. `{genre}/{albumartist}/{album}/...`
- Sanitization strips characters that are illegal on any common filesystem (Windows, macOS, Linux, SMB shares), trims trailing dots and spaces, and caps segment length.
- Collisions get a deterministic suffix and are never overwritten.
- A template change does **not** move files automatically. It produces a dry-run reorganize plan, which needs confirmation ([data safety](data-safety.md)).

> [!question] Default template and genre source
> Confirm the default template. For genre-based layouts, choose the genre source (MB tags, Last.fm tags, or a user mapping). Genre is noisy, and a track can only live in one folder.

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
- If a recording doesn't exist on the canonical release (a bonus track, say), it is filed under the canonical album folder with its own release MBID kept in tags and flagged as a bonus. The user can switch the canonical release to a deluxe edition instead.
- Changing a release group's canonical release produces a dry-run merge plan.

## Fragmentation report
- Lists release groups whose owned tracks span more than one release, including anything imported before canonicalization existed.
- Per group: the current releases, the proposed canonical release, and the files that would be retagged or moved.
- **Merge** = dry-run preview → confirm → journaled retag and move (undoable).

## Cover art
- Fetched from the Cover Art Archive for the canonical release, falling back to the release group.
- Saved as `cover.jpg` in the album folder and optionally embedded (a per-library setting).
