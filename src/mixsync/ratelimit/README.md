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
| `musicbrainz` | 1 req/s | entity 7 d, search 1 d | `MixSync/<ver> ( <contact> )` |
| `coverart` | 1 req/s | 30 d | same User-Agent |
| `acoustid` | 3 req/s | lookup 7 d, submit never | app key |
| `listenbrainz` | from `X-RateLimit-*` headers | 1 h | user token |
| `lastfm` | 5 req/s | 1 d | app key |
| `slskd`, `navidrome` | unlimited (local) | none | API key / user |

## Rules
- `429` / `503` → honor `Retry-After`, else exponential backoff with jitter (1 s → 2 s → 4 s … cap 5 min). Set `blocked_until` on the bucket so every worker pauses.
- Never cache authenticated or POST responses.
- Contact for the User-Agent comes from `Settings.mb_contact`. Startup fails if it is unset and MusicBrainz is enabled.
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
