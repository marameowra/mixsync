# Unverified imports and MusicBrainz submission

**Problem:** soulsync refuses to import tracks that aren't on MusicBrainz, and gives no way to add them to MB.

## Unverified import
- From the review screen, **Mark unverified** imports the file even without a confident MB match.
- Tags come from:
  1. user edits in the review form,
  2. read-only fallback sources (Discogs, Deezer) offered as suggestions, or
  3. the file's existing tags.
- The file is filed under the normal [path template](library.md#path-templates), tagged `MIXSYNC_STATUS=unverified`, and flagged in the DB.
- The library view has an "Unverified" filter and a count.

> [!question] Fallback sources
> Discogs requires a user token and has its own rate limits. Check Deezer's API terms for this use. Both are optional and off by default until confirmed.

## Submit to MusicBrainz
The **Submit to MusicBrainz** button on any unverified item picks a path:

| Situation | Action |
|---|---|
| The release is on a store or streaming service (Bandcamp, Deezer, iTunes, Spotify, etc.) | Open **Harmony** with the release URL. Harmony gathers the metadata and seeds the MB editor. |
| Not on any store | Generate an **MB release-editor seed**: an auto-submitting HTML form that POSTs to `https://musicbrainz.org/release/add` with artist, title, track list, durations, label, date, and barcode if known. It opens in a new tab. |

- The user reviews and submits the edit in MB with their own account. MixSync never submits edits for them.
- The seed includes an edit note crediting MixSync and saying the data came from local files.

> [!question] Seeding field names
> Verify the field names against the current MB "Release Editor Seeding" documentation before implementing.

## Re-check and promote
- A background job re-checks unverified items about weekly (rate limited, jittered). It searches MB by artist and title and checks AcoustID.
- When a confident match appears, the item goes to the review queue as **Promote to verified**, with a diff of the tag changes. The user confirms.
- After promotion, the fingerprint is **submitted to AcoustID** linked to the new recording MBID. This needs an AcoustID user API key in the user's settings.
