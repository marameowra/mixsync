# `targets`

## Purpose
Adapters for the streaming servers that serve the library. v1 supports **Navidrome** through the Subsonic API. Jellyfin, Plex, and Music Assistant come later.

## Owns / does not own
- **Owns:** rescans, playlist create/update, reading stars and play counts per user.
- **Does not own:** streaming or playback, or the files themselves.

## Proposed files
| File | Contents |
|---|---|
| `navidrome.py` | `NavidromeTarget` implementing `LibraryTarget` via the Subsonic API (token + salt auth) |
| `subsonic.py` | Minimal typed Subsonic client. Built: `ping`, `startScan`, `getScanStatus`. Later (phase 6): `createPlaylist`, `updatePlaylist`, `getStarred2`, `search3`, `getSong` |

## Key interface (`core.protocols.LibraryTarget`)
```
rescan() -> None                              # built (phase 2); startScan needs an admin user
rescan(library) -> None                       # phase 6: library = main | discover
push_playlist(user, name, track_paths) -> external_id   # phase 6
get_stars(user) -> set[track_path]                      # phase 6
get_play_counts(user, paths) -> dict[path, int]         # phase 6
```
Errors: transport/5xx → `TransientError`; Subsonic codes 40/41 (credentials) and 50 (not admin) → `PermanentError`. The password and token never appear in messages, and Navidrome responses are never cached.

## Rules
- Act per user: playlists and stars use the user's linked Navidrome credentials (`user_links`), not an admin account.
- Map tracks by path relative to the library root. Navidrome's song ids are cached but never trusted across rescans.
- Debounce rescans: at most one queued per library, coalescing imports that land within 30 s.

> [!question] Navidrome multi-library
> Confirm that the deployed Navidrome version supports multiple libraries (0.58+). If not, `discover` tracks live in the main library and are identified by playlist only.

## May import from
`core`, `ratelimit`.

## Tests
- `tests/unit/targets/`: Subsonic auth token generation; response mapping with recorded JSON.
- `tests/integration/`: real Navidrome container: import → rescan → track visible → playlist pushed.

## Design docs
[Architecture](../../../docs/architecture.md#adapter-interfaces) · [Discovery and retention](../../../docs/design/discovery-retention.md)
