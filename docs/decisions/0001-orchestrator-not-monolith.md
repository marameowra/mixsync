# ADR 0001: Orchestrator, not monolith

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
The tool being replaced (soulsync) tries to do everything itself, and that is where its bloat comes from. Several of its problems, such as broken Soulseek chat rooms, come from re-implementing things other tools already do well. The project guidelines say to use prior art and to do a limited set of things very well.

## Decision
MixSync owns **the decisions**: requests, matching, library policy, playlists, retention, users, and the UI. It delegates **the heavy lifting** to mature tools over their APIs: slskd, MusicBrainz, AcoustID, mutagen, Navidrome, ListenBrainz, and Last.fm. Each external system sits behind a small adapter interface (`DownloadSource`, `MetadataProvider`, `LibraryTarget`, `HistorySource`).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Orchestrator (chosen)** | Much less code; battle-tested protocols (the Soulseek protocol is especially hard to get right); each part is swappable; stays small | More containers to run; depends on upstream API stability |
| Monolith (own Soulseek client, own tagging, own player) | One thing to deploy; full control | Huge scope; repeats soulsync's mistakes; worse than the specialist tools at every task |
| Lidarr plugin / Lidarr-centric | Mature request/watchlist model | C#/.NET, a heavy codebase; Lidarr's own metadata server has had outages; you inherit its UI and its bloat |

## Consequences
- MixSync depends on slskd and Navidrome APIs. Mitigation: thin adapters plus contract tests against real containers ([testing](../testing.md#integration-tests-compose)).
- Soulseek chat and rooms are used through slskd's web UI. MixSync can link to it but does not rebuild it.
- Adding a source or target means writing one adapter, not touching core code.
