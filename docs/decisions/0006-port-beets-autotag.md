# ADR 0006: Port beets' matching algorithm instead of embedding it

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
[ADR 0005](0005-explainable-matching-score.md) models the scorer on beets' `autotag` distance. Phase 2 was to open with a spike deciding whether to call `beets.autotag` directly or port its algorithm ([matching](../design/matching.md#beets-reuse)). The matcher is the part of MixSync most likely to need changes: per-user thresholds, new features such as pre-download evidence, and calibrated weights.

## Decision
**Port the algorithm** into `match/` (`features.py`, `scorer.py`) and do not depend on beets. No spike is needed.

- Port only the distance and candidate logic. Leave beets' library DB, config, plugins, and MusicBrainz client behind.
- beets is MIT licensed. Keep its copyright and permission notice in each ported file's header and credit beets in the module README.
- Record the beets commit the port is based on, so later upstream fixes can be compared.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Port (chosen)** | Free to modify; no MusicBrainz requests outside MixSync's limiter; no beets config or DB; one less large dependency | We maintain the code; upstream fixes must be copied by hand |
| Embed `beets.autotag` | Most reuse; upstream fixes for free | Couples MixSync to beets internals and config; beets' own MB client could bypass the rate limiter; changes to scoring need upstream changes or monkeypatching |

## Consequences
- `match` imports only `core` and `rapidfuzz`. beets is not a dependency.
- Ported code follows MixSync's rules: pure, typed under pyright strict, each penalty with a reason string, versioned by `SCORER_VERSION`.
- The regression suite, not beets' behaviour, is the reference for correctness once it exists.
