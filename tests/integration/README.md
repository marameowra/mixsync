# `tests/integration`

End-to-end tests against real containers: slskd, Navidrome and MixSync.

## Rules
- Marked `@pytest.mark.integration`; skipped unless `-m integration`. Runs nightly and on demand, not on every push.
- CI uses a fake slskd, not the Soulseek network ([ADR 0016](../../docs/decisions/0016-fake-slskd-in-ci.md)). The fake must reproduce the [edge-case catalog](../fixtures/README.md#soulseek-edge-case-catalog).

> Planned — not built: the compose file and the pipeline, discovery and crash-restart scenarios. Nothing runs under this folder yet.

## Design doc
[Testing: integration](../../docs/testing.md#integration-tests-compose)
