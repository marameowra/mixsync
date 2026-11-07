# `discovery`

## Purpose
Per-user playlists built from listening history, including tracks not yet owned, plus the retention rules that decide whether those provisional tracks stay.

## Owns / does not own
- **Owns:** the ListenBrainz adapter (`listenbrainz.py`, a `HistorySource`), playlist building (`playlists.py`; unowned tracks become `Request(provisional=True, source=discovery)`), keep/expire decisions (`retention.py`).
- **Does not own:** downloading (the normal request pipeline), file moves (`library.fileops`), or Navidrome calls (the injected `LibraryTarget`).

## Rules / invariants
- Two kinds of code, kept in separate files: the adapter follows the adapter rule; the services get their adapters injected as Protocols.
- A kept track is promoted: copied through `Importer.import_release` into the main library, then the discover copy is trashed, both journaled. A recording already in `tracks` is not imported again. Kept holds stay `kept` until promoted, so a failed promote retries the next day.
- A track is trashed only when every user's row is expired, never while any user still wants it. Expiry goes through `library.fileops.trash()`; nothing is deleted outright.
- Stars and play counts are fetched once per user per run. A user without a working Navidrome link is skipped, with a note on their playlists.
- `DISCOVERY_RETAIN` runs daily per user (jittered, self-rescheduling, started in `start_chains`). A run that promoted or expired anything queues a rescan and a `DISCOVERY_PUSH` per affected playlist.
- Defaults (retention, keep threshold, refresh, size, unowned share) live in the [discovery design](../../../docs/design/discovery-retention.md).

## May import
- `listenbrainz`: `core`, `ratelimit`.
- `playlists`, `retention`: `core`, `db`, `match`, `library`.

## Design doc
[Discovery and retention](../../../docs/design/discovery-retention.md)
