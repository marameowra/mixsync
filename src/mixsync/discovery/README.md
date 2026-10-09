# `discovery`

## Purpose
Per-user playlists built from listening history, including tracks not yet owned, plus the retention rules that decide whether those provisional tracks stay.

This folder holds **two kinds of code**. Keep them in separate files:
- **Adapters** (`listenbrainz.py`, `lastfm.py`) implement `HistorySource`. They follow the adapter rule: `core` + `ratelimit` only.
- **Services** (`playlists.py`, `retention.py`) follow the service rule: `core`, `db`, `match`, `library`, with adapters injected as Protocols.

## Owns / does not own
- **Owns:** fetching recommendations, building playlist contents, creating provisional requests, keep/expire decisions.
- **Does not own:** downloading (normal request pipeline), file moves (`library.fileops`), or Navidrome calls (the injected `LibraryTarget`).

## Proposed files
| File | Kind | Contents |
|---|---|---|
| `listenbrainz.py` | adapter | Recommendations, LB Radio prompts, top artists (`liblistenbrainz` or httpx) |
| `lastfm.py` | adapter | Similar artists/tracks, top artists (`pylast` or httpx) |
| `playlists.py` | service | `build(playlist_def, user) -> list[PlaylistEntry]`; owned resolved by recording MBID; unowned → `Request(provisional=True, source=discovery)` |
| `retention.py` | service | Daily job: evaluate keep rules, promote kept tracks, trash expired ones |

## Key interfaces
```
HistorySource (core.protocols):
  recommendations(user, limit) -> list[RecordingRef]
  similar(seed: ArtistRef | RecordingRef, limit) -> list[RecordingRef]
  top_artists(user, period) -> list[ArtistRef]

Playlist definition (stored as JSON): sources[], size=50, unowned_ratio=0.3, cadence=weekly
```

## Retention rules
```
for each provisional_tracks row not kept and not expired:
  kept if: starred by that user  OR  plays >= N (default 3)  OR  manual keep
  if kept: library.importer.promote(track)   # discover → main, with canonicalization
  elif now >= expires_at: mark expired for this user
a track is trashed only when every user's row is expired (never while any user still wants it)
```
- Expiry → `library.fileops.trash()`. Never deleted outright.
- A "Expiring in 7 days" digest query backs the UI page.

> [!question] Defaults
> 30-day retention, keep after 3 plays, weekly refresh, 50 tracks with 30% unowned. See [discovery design](../../../docs/design/discovery-retention.md).

## May import from
- Adapters: `core`, `ratelimit`.
- Services: `core`, `db`, `match`, `library`.

## Tests
- Playlist builder with fake `HistorySource`: ratio respected, owned tracks resolved, no duplicates.
- Retention with a fake clock and fake `LibraryTarget`: star → promote; plays → promote; expiry → trash; multi-user keep.

## Design docs
[Discovery and retention](../../../docs/design/discovery-retention.md)
