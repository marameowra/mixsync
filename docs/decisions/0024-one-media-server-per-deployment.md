# ADR 0024: One media server per deployment, matched by recording MBID

- **Status:** Accepted
- **Date:** 2026-11-11

## Context
Navidrome is the only media server, and its name is built into the settings (`navidrome_*`), the playlist and provisional-track columns (`navidrome_playlist_id`, `navidrome_song_id`) and user links (`service = "navidrome"`). #302 adds Jellyfin and Plex. Supporting more than one server raises three questions: whether a deployment can use several at once, what happens to stored server ids when the server changes, and how mixsync finds its tracks on a server it doesn't control.

## Decision
- A deployment hands off to exactly one media server, chosen by an app setting (`navidrome`, `jellyfin` or `plex`).
- Server-specific names become generic: settings `media_server_*`, columns `server_playlist_id` and `server_song_id`, and `UserLink.service` holds the server kind. One migration renames them and keeps existing values.
- Switching the media server, after the admin confirms, clears stored playlist and song ids (the next push creates playlists again, and tracks are looked up again) and marks every user's link `needs_relink`. Nothing on disk changes.
- Every user links their own account on the media server. Stars, plays and playlists belong to that account. An admin credential is used only for rescans. Passwords are exchanged for tokens where the server allows it (Jellyfin, Plex).
- A track is found on the server only when the server reports the same recording MBID. mixsync never guesses by title or by file path.
- Every media server must report stars and play counts. There are no capability flags.
- Rescans cover the whole library. Scanning only the imported folder is a later optimization.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **One server (chosen)** | Each adapter stays simple; one source for stars and plays | No running Jellyfin and Navidrome side by side |
| Several, one primary | Hand-off to every server | Playlist pushes and links for each server; deferred to a follow-up |
| Several, merged signals | Stars and plays from everywhere | Users and tracks must be matched across servers |
| **Generic columns (chosen)** | One schema for every server | A rename migration |
| Separate columns per server | No rename | Three mostly empty sets of columns |
| **Reset on switch (chosen)** | Stale ids never reach the new server | Users relink, and playlists are created again |
| Block switching | Nothing is lost | No way to move servers |
| **Match by MBID (chosen)** | Never attaches the wrong track | Tracks without an MBID on the server go unmatched |
| Match by path | Works without MBIDs | Container mount paths differ, and Navidrome reports fake paths |

## Consequences
- Plex's support for recording MBIDs, ratings and play counts is undocumented. A test on a real server must confirm them before the Plex adapter is built. If it fails, this ADR is revisited.
- Music Assistant is not a media server under this model. It fronts other servers, so it is tracked separately.
- A capability system for servers that lack stars or plays is not needed until such a server is added.
