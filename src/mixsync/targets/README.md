# `targets`

## Purpose
Adapters for the servers that serve the library. v1 supports Navidrome through the Subsonic API; Jellyfin, Plex and Music Assistant come later.

## Owns / does not own
- **Owns:** `NavidromeTarget` (a `LibraryTarget`), the minimal typed Subsonic client (`subsonic.py`), rescans, playlist push, stars and play counts.
- **Does not own:** streaming or playback, or the files themselves.

## Rules / invariants
- Errors: transport and 5xx are `TransientError`; Subsonic codes 40/41 (credentials) and 50 (not admin) are `PermanentError`. Passwords and tokens never appear in messages, and Navidrome responses are never cached.
- Per-user actions (playlists, stars) use the user's linked credentials (`user_links`), not an admin account. `startScan` needs an admin user.
- Map tracks by path relative to the library root; Navidrome song ids are never trusted across rescans.
- Debounce rescans: at most one queued per library, coalescing imports within 30 s.

## May import
`core`, `ratelimit`.

## Design doc
[Architecture](../../../docs/architecture.md#adapter-interfaces) · [Discovery and retention](../../../docs/design/discovery-retention.md)
