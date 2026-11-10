# `discovery`

## Purpose
Per-user playlists built from listening history, including tracks not yet owned, plus the retention rules that decide whether those provisional tracks stay.

## Owns / does not own
- **Owns:** the ListenBrainz adapter (`listenbrainz.py`, a `HistorySource`), playlist building (`playlists.py`; unowned tracks become `Request(provisional=True, source=discovery)`), keep/expire decisions (`retention.py`), and the shared playlist track resolver (`resolver.py`: ISRC via MusicBrainz, else ListenBrainz lookup, accepted only at the matcher's balanced auto-accept band; otherwise unresolved, never guessed), and mirrored playlists (`mirrored.py`: per-user copies of upstream playlists; adapters implement `Upstream` and call `create`; missing tracks become `Request(source=playlist)` only on the owner's confirmation), and the Spotify-file upstream (`spotify_file.py`: parses a data-export JSON or Exportify CSV; the last uploaded track list is kept as JSON under `config_dir/spotify`, no Spotify API), the Spotify-API upstream (`spotify_api.py`: PKCE OAuth, the user's own and collaborative playlists, a one-shot import on request stored as the mirror's entries; scheduled syncs never call Spotify; its tracks never become a request; disconnect deletes the link and these mirrors), and smart playlists (`smart.py`: rules over the owned library; never a request).
- **Does not own:** downloading (the normal request pipeline), file moves (`library.fileops`), or Navidrome calls (the injected `LibraryTarget`).

## Rules / invariants
- Two kinds of code, kept in separate files: the adapter follows the adapter rule; the services get their adapters injected as Protocols.
- A kept track is promoted: copied through `Importer.import_release` into the main library, then the discover copy is trashed, both journaled. A recording already in `tracks` is not imported again. Kept holds stay `kept` until promoted, so a failed promote retries the next day.
- A track is trashed only when every user's row is expired, never while any user still wants it. Expiry goes through `library.fileops.trash()`; nothing is deleted outright.
- Stars and play counts are fetched once per user per run. A user without a working Navidrome link is skipped, with a note on their playlists.
- `DISCOVERY_RETAIN` runs daily per user (jittered, self-rescheduling, started in `start_chains`). A run that promoted or expired anything queues a rescan and a `DISCOVERY_PUSH` per affected playlist.
- Defaults (retention, keep threshold, refresh, size, unowned share) live in the [discovery design](../../../docs/design/discovery-retention.md).
- A mirrored sync replaces the Navidrome playlist with the owned, resolved tracks in upstream order. A track leaving upstream leaves the playlist only. Unresolved tracks are listed, never pushed, and resolved again each sync; resolved ones are remembered. An empty upstream list never wipes a mirror that has tracks.
- A `NEVER_REQUEST` kind (`spotify_api`, per Spotify's terms) never makes a request: `request_missing` raises and the page offers none.
- A smart build takes only owned tracks (a `tracks` row) that Navidrome lists in the main library, read in one paged `search3` walk (stars, last played, genre per song), never one call per track. It never requests and never touches a file. If Navidrome lists none of the owned tracks (not scanned yet), the last playlist is kept.

## May import
- `listenbrainz`: `core`, `ratelimit`.
- `resolver`: `core`, `match`, `metadata`, `listenbrainz`.
- `playlists`, `retention`: `core`, `db`, `match`, `library`.
- `smart`: `core`, `db`, `discovery.playlists`.
- `spotify_file`: `core`, `db`, `discovery.mirrored`.
- `spotify_api`: `core`, `db`, `ratelimit`, `discovery.mirrored`.
- `mirrored`: `core`, `db`, `acquire.lifecycle`, `acquire.requests`, `discovery.playlists` (the resolver and adapters are injected).

## Design doc
[Discovery and retention](../../../docs/design/discovery-retention.md)
