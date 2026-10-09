# `ratelimit`

## Purpose
The only way MixSync talks HTTP to the outside world. It provides per-service rate limiting, a response cache, a correct User-Agent, and retry/backoff, so no adapter can accidentally hammer a service.

## Owns / does not own
- **Owns:** the shared `httpx.AsyncClient` factory, token buckets, cache, `Retry-After` handling, User-Agent.
- **Does not own:** API semantics. Adapters build requests; this module sends them politely.

## Proposed files
| File | Contents |
|---|---|
| `client.py` | `polite_client(service) -> PoliteClient`, an httpx wrapper that acquires a token, checks the cache, sends, and backs off |
| `bucket.py` | Token bucket with state in the `rate_buckets` table, shared across worker processes |
| `cache.py` | Response cache (hishel with SQLite storage, or the `http_cache` table) with per-service TTLs |
| `services.py` | The limits and TTL table (below), plus User-Agent construction |

## Limits and TTLs
| Service | Rate | Cache TTL | Identity |
|---|---|---|---|
| `musicbrainz` | 1 req/s per IP (`MIXSYNC_MB_RATE`, ≤ 1) | entity 7 d, search 1 d | `MixSync/<ver> ( <contact> )` |
| `coverart` | 1 req/s (self-imposed; none published) | art saved to disk | same User-Agent; **follow 307 redirects** (off by default in httpx) |
| `acoustid` | 3 req/s | lookup 7 d, submit never | app key |
| `listenbrainz` | 1 req/s, tightened by `X-RateLimit-Remaining` / `-Reset-In` | 1 h | UA required; `Authorization: Token` |
| `discogs` (off by default) | 50/min (documented: 60/min per IP) | 1 d | unique UA; user token |
| `deezer` (off by default) | 1 req/s (self-imposed) | 1 d | UA |
| `slskd`, `navidrome` | unlimited (local) | none | API key / user |

Every number here is checked against the services' docs; see [services etiquette](../../../docs/design/services-etiquette.md#summary) for sources.

## Rules
- `429` / `503` → honor `Retry-After`, else exponential backoff with jitter (1 s → 2 s → 4 s … cap 5 min). Set `blocked_until` on the bucket so every worker pauses. MusicBrainz signals overload with a bare **503 and no retry header**, so the backoff path is the normal one there.
- **Never send a library default User-Agent** (`python-httpx/…`, `python-musicbrainz/…`). MusicBrainz throttles anonymous and shared agents. A unit test asserts every `PoliteClient` sets the MixSync UA.
- MusicBrainz's 1 req/s applies to the **whole source IP**, so it's shared with other MB tools on the network. `MIXSYNC_MB_RATE` can lower it (e.g. 0.5) and is rejected if set above 1.
- Never cache authenticated or POST responses.
- Contact for the User-Agent comes from `Settings.mb_contact`, which defaults to `https://github.com/marameowra/mixsync`. Startup fails if it is set to an empty string.
- A bucket in the DB means separate worker containers share one budget.

> [!question] Bucket contention
> A DB round-trip per token is fine at 1–5 req/s. If profiling shows lock contention on SQLite, keep an in-process bucket and reserve tokens from the DB in batches.

## May import from
`core`, `db`.

## Tests
- `tests/unit/ratelimit/`: bucket math with a fake clock; two concurrent clients never exceed the rate; a 429 with `Retry-After: 10` blocks for 10 s.
- Cache hit/miss/expiry with respx-mocked responses.

## Design docs
[Respecting external services](../../../docs/design/services-etiquette.md)
