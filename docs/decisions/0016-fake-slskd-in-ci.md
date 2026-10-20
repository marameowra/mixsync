# ADR 0016: Integration tests use a fake slskd

- **Status:** Accepted, not built yet (no integration tests or fake slskd exist; see [testing](../testing.md))
- **Date:** 2026-10-20

## Context
Integration tests run against real containers. The Soulseek network is non-deterministic and slow, and CI should not load it.

## Decision
CI replaces slskd with a fake that serves fixture search results and files. The real network is for manual runs only (`-m integration --real-soulseek`). The fake must reproduce every case in the [edge-case catalog](../../tests/fixtures/README.md#soulseek-edge-case-catalog): slow peers, failed transfers, disconnects, empty results.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Fake slskd (chosen)** | Deterministic; no load on Soulseek | The fake must be kept faithful |
| Real network in CI | Real behaviour | Flaky, slow, impolite to the network |

## Consequences
- A new real-world failure becomes a new catalog ID and a fake behaviour.
