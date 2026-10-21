# MixSync documentation

MixSync is a self-hosted tool that replaces a music streaming service with your own files. It takes a request, finds the music (Soulseek first), verifies the files against MusicBrainz and AcoustID, files them into a clean library, and hands that library to a streaming server (Navidrome first). It also builds discovery playlists from your listening, with a retention window for tracks you don't own yet. Anything a mature tool already does well (Soulseek networking, tag I/O, streaming, recommendations), MixSync delegates.

## Status
Released: v1.0.1 (`version` in `pyproject.toml`). GitHub issues are the roadmap. Features not built yet are marked `Planned — not built (#N)` in the doc that owns them. Specs describe intended behavior; where code and a spec disagree, fix whichever is wrong.

**v1 scope**
- Soulseek (slskd) → verify → import → Navidrome; release-group canonicalization and fragmentation report
- Unverified import and MusicBrainz submission
- Re-import of an existing library; artist/label watchlist
- Discovery playlists with retention for not-yet-owned tracks
- Per-user matching profiles; `admin` and `user` roles only

**After v1:** approval queue and role editor; Prowlarr/qBittorrent/SABnzbd/yt-dlp sources; Jellyfin/Plex/Music Assistant targets; stats page; Last.fm ([why not](design/services-etiquette.md#lastfm)). **Out of scope:** a Soulseek client, chat or rooms (slskd covers them); a streaming server or player; anything outside request → acquire → verify → organize → stream → discover.

## Index
- [Architecture](architecture.md): components, pipeline, tech stack
- [Testing](testing.md): how changes are validated
- [Local development permissions](agents/local-development-permissions.md)
- Agent docs: [domain](agents/domain.md), [issue tracker](agents/issue-tracker.md); triage labels are in [CLAUDE.md](../CLAUDE.md)
- [Workflow prior art](research/workflow-prior-art.md): what Lidarr, soularr and similar tools get wrong
- [Deploy](../deploy/README.md) · UI: [design system](../design/system/README.md)

**Decisions (ADRs; bodies are immutable)**
- [0001 Orchestrator, not monolith](decisions/0001-orchestrator-not-monolith.md)
- [0002 Python backend](decisions/0002-python-backend.md)
- [0003 Server-rendered UI with HTMX](decisions/0003-htmx-server-rendered-ui.md)
- [0004 SQLite and a DB-backed job queue](decisions/0004-sqlite-and-db-job-queue.md)
- [0005 Explainable matching score](decisions/0005-explainable-matching-score.md)
- [0006 Port beets' matching algorithm](decisions/0006-port-beets-autotag.md)
- [0007 Quarantine owned by MixSync](decisions/0007-quarantine-owned-by-mixsync.md)
- [0008 Trash purges automatically](decisions/0008-trash-auto-purge.md)
- [0009 AcoustID checks every file](decisions/0009-acoustid-mandatory.md)

**Design**
- [Matching](design/matching.md) · [Library and canonicalization](design/library.md) · [Data safety](design/data-safety.md)
- [Unverified imports and MB submission](design/unverified-and-mb-submit.md) · [Watchlist](design/watchlist.md) · [Discovery and retention](design/discovery-retention.md)
- [Migration](design/migration.md) · [External services](design/services-etiquette.md)

## Principles
1. **Use prior art.** Research before building; delegate to mature tools.
2. **Data safety first.** No silent destructive actions: "delete" means trash, with a journal and an undo window ([data safety](design/data-safety.md)). The one automatic delete is the trash purge ([ADR 0008](decisions/0008-trash-auto-purge.md)); bulk changes get a dry-run plan.
3. **Be a good citizen.** Rate-limit, cache, identify yourself ([services](design/services-etiquette.md)).
4. **Stay small.** Every feature serves request → acquire → verify → organize → stream → discover.
5. **Restrained UI.** Responsive, no flashy animation, works with password managers.
