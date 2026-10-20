# `ratelimit`

## Purpose
The only way MixSync talks HTTP to the outside world: per-service rate limits, a response cache, the User-Agent, and retry/backoff, so no adapter can hammer a service.

## Owns / does not own
- **Owns:** `PoliteClient`, the token buckets (`bucket.py`, state in the DB), the cache (`cache.py`), and the per-service limits and TTL table (`services.py`).
- **Does not own:** API semantics. Adapters build requests; this package sends them politely.

## Rules / invariants
- Limits, TTLs and identity per service live in [services etiquette](../../../docs/design/services-etiquette.md#summary) and `services.py`; this README does not repeat them.
- `429` / `503` honor `Retry-After`, else back off exponentially with jitter, and set `blocked_until` on the bucket so every worker pauses. MusicBrainz signals overload with a bare 503, so that path is the normal one there.
- Never send a library default User-Agent. A unit test asserts every `PoliteClient` sets the MixSync one.
- Never cache authenticated or POST responses.
- Buckets live in the DB so separate worker containers share one budget.
- `MIXSYNC_MB_RATE` can lower the MusicBrainz rate and is rejected above 1. `Settings.mb_contact` must not be empty.

## May import
`core`, `db`.

## Design doc
[Respecting external services](../../../docs/design/services-etiquette.md)
