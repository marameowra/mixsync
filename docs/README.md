# MixSync documentation

MixSync is a self-hosted tool that replaces a music streaming service with your own files. It takes a request, finds the music (Soulseek first), verifies the files are what they claim to be, files them into a clean library, and hands that library to a streaming server (Navidrome first). It also builds discovery playlists from your listening, including tracks you don't own yet, and gives those tracks a retention window so you can decide whether to keep them.

It does a few things and does them well. Anything a mature tool already does well (Soulseek networking, tag I/O, streaming, recommendations), MixSync delegates.

## Status
Design phase. No code yet. These docs are the source of truth for v1.

## Start here
- [Architecture](architecture.md): how the pieces fit, repo layout, deployment
- [Roadmap](roadmap.md): v1 scope, build order, what's deferred
- [Testing](testing.md): how changes are validated

## Decisions (ADRs)
- [0001 Orchestrator, not monolith](decisions/0001-orchestrator-not-monolith.md)
- [0002 Python backend](decisions/0002-python-backend.md)
- [0003 Server-rendered UI with HTMX](decisions/0003-htmx-server-rendered-ui.md)
- [0004 SQLite and a DB-backed job queue](decisions/0004-sqlite-and-db-job-queue.md)
- [0005 Explainable matching score](decisions/0005-explainable-matching-score.md)

## Design
- [Matching](design/matching.md)
- [Library layout and release canonicalization](design/library.md)
- [Data safety](design/data-safety.md)
- [Unverified imports and MusicBrainz submission](design/unverified-and-mb-submit.md)
- [Watchlist](design/watchlist.md)
- [Discovery and retention](design/discovery-retention.md)
- [Migration from an existing library](design/migration.md)
- [Respecting external services](design/services-etiquette.md)

## Guiding principles
1. **Use prior art.** Research before building. Delegate to mature tools.
2. **Data safety first.** No silent destructive actions. Anything risky gets a dry-run, a confirmation, and an undo path.
3. **Be a good citizen.** Rate-limit, cache, identify yourself, and share back.
4. **Stay small.** Every feature must serve request → acquire → verify → organize → stream → discover.
5. **Restrained UI.** Responsive, reflows on any screen, no flashy animation, works with password managers.
