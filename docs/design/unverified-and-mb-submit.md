# Unverified imports and MusicBrainz submission

Soulsync refuses tracks that aren't on MusicBrainz and offers no way to add them. MixSync imports them as unverified and helps submit them.

## Unverified import
- **Mark unverified** on the review screen imports without a confident MB match. Tags come from the user's edits, suggestions from fallback sources, or the file's existing tags.
- Filed under the normal [path template](library.md#path-templates), tagged `MIXSYNC_STATUS=unverified`, flagged in the DB; the library view has an Unverified filter and count.

> Planned — not built (#223): Discogs and Deezer as read-only suggestion sources, off by default ([terms](services-etiquette.md#discogs-and-deezer-fallbacks-off-by-default)).

## Submit to MusicBrainz
The **Submit to MusicBrainz** button picks a path:
- Release on a store or streaming service (Bandcamp, Deezer, iTunes, Spotify...): open **Harmony** with the URL, which seeds the MB editor.
- Otherwise: an **MB release-editor seed**, an HTML form submitted by the user's click (never on page load) that POSTs to `https://musicbrainz.org/release/add` in a new tab, with artist, title, tracks, durations, label, date and barcode if known. Field names verified 2026-10-10 against the [seeding docs](https://musicbrainz.org/doc/Development/Release_Editor_Seeding).

The user reviews and submits the edit with their own account; MixSync never submits for them. The seed's edit note credits MixSync and says the data came from local files.

## Re-check and promote
- **Check MusicBrainz now** (per item or album) is the primary trigger, used after the seeded edit is applied.
- Background re-checks back off, since MB asks clients not to poll: 1 week → 2 weeks → 1 month → quarterly, each at a random point in its interval. One search per album, then AcoustID lookups only for that album's files.
- A confident match enters the review queue as **Promote to verified** with a tag diff; the user confirms.
- After promotion the fingerprint is submitted to AcoustID with the new recording MBID (key rules: [services](services-etiquette.md#acoustid)). Submissions are batched per plan and polled up to three times (1 min, 10 min, 1 h). Undo does not retract them: AcoustID has no retract.
