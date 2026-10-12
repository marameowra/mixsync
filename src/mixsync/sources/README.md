# `sources`

## Purpose
Adapters that find and download music. v1 has one: **slskd** (Soulseek). Later: Prowlarr + qBittorrent, Prowlarr + SABnzbd, yt-dlp.

## Owns / does not own
- **Owns:** translating `DownloadSource` calls into the slskd REST API; mapping slskd responses to `core` types; search pacing.
- **Does not own:** choosing a candidate (that's `match`), moving files into the library (`library`), the Soulseek protocol, chat, or rooms (slskd does these).

## Files
| File | Contents |
|---|---|
| `slskd.py` | `SlskdSource` implementing `DownloadSource` |
| `query.py` | `queries(artist, album)`: search strings (artist + album, with fallbacks: strip punctuation, drop "feat.", album only) |

## Key interface (`core.protocols.DownloadSource`)
```
search(query) -> list[Candidate]            # stage-1 input; one Candidate per peer folder
enqueue(candidate)                          # queue every audio file of the folder
status(candidate) -> TransferInfo           # queued / in_progress / done / failed, bytes
active_downloads() -> {transfer id: bytes}  # every download moving data now (slskd `InProgress`)
```

## Rules
- **At most 2 concurrent searches** (configurable). Identical queries within 10 minutes are debounced to the first result.
- A query that returns nothing backs off: 1 h → 6 h → 24 h → weekly. Store the backoff state on the request.
- **At most 3 concurrent downloads** (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`), counting only `InProgress` transfers; ones slow for a whole window stop counting (see [etiquette](../../../docs/design/services-etiquette.md#soulseek)). `active_downloads()` feeds `acquire.gate.DownloadGate`, which keeps the `bytesTransferred` samples in the worker's memory (only the worker starts downloads) and derives the window average from them. A full cap defers the `download` job (re-queued with a short `run_after`, no attempt spent). It is checked only when a job would start a transfer, and a just-started transfer still waiting in the peer's queue is not counted, so the cap can briefly overshoot.
- Never queue more than one album folder per peer at a time.
- Downloads land in `/downloads` (slskd's folder). This module never writes to `/data`.
- slskd API key comes from `Settings`; talk to slskd through `ratelimit.polite_client("slskd")` (no limit, but uniform retries and logging).

- **Not built yet:** the search concurrency cap, the debounce, the no-results backoff (they need per-request DB state), and `release()`. (`TransferInfo.local_path` is always `None`: slskd's API does not report it.) Transfer monitoring is just the `download` job's own status poll plus the cap's rate samples.
- **Client library:** `slskd-api` on PyPI is a sync, untyped `requests` wrapper, so `slskd.py` is a thin httpx client over `PoliteClient`.

## May import from
`core`, `ratelimit`.

## Tests
- `tests/unit/sources/`: slskd response → `Candidate` mapping using hand-built JSON (not recorded) in `tests/fixtures/slskd/`; query builder cases (unicode, "feat.", punctuation).
- `tests/integration/`: real slskd container (see [testing](../../../docs/testing.md#integration-tests-compose)).

## Design docs
[Architecture](../../../docs/architecture.md#components-and-delegation) · [Respecting services: Soulseek](../../../docs/design/services-etiquette.md#soulseek)
