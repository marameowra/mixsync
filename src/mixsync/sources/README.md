# `sources`

## Purpose
Adapters that find and download music. v1 has one: slskd (Soulseek). Torrent, Usenet and yt-dlp sources come later.

## Owns / does not own
- **Owns:** `SlskdSource` (a `DownloadSource` over the slskd REST API), the response mapping to `core` types, and the search query builder (`query.py`).
- **Does not own:** choosing a candidate (`match`), moving files into the library (`library`), or the Soulseek protocol (slskd does that).

## Rules / invariants
- Downloads land in `/downloads` (slskd's folder). This package never writes to `/data`.
- Talk to slskd through `ratelimit.PoliteClient("slskd", ...)`. `slskd-api` is a sync, untyped wrapper, so `slskd.py` is a thin httpx client.
- At most 3 concurrent downloads (`MIXSYNC_MAX_CONCURRENT_DOWNLOADS`), counting only `InProgress` file transfers; slow ones stop counting ([etiquette](../../../docs/design/services-etiquette.md#soulseek)). `active_downloads()` feeds `acquire.gate.DownloadGate`.
- Never queue more than one album folder per peer at a time.
- `TransferInfo.local_path` is always `None`: slskd's API does not report it.
  > Not built yet (no issue filed): the search concurrency cap, query debounce and no-results backoff, and `release()`.

## May import
`core`, `ratelimit`.

## Design doc
[Architecture](../../../docs/architecture.md#components-and-delegation) · [Respecting services: Soulseek](../../../docs/design/services-etiquette.md#soulseek)
