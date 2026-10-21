# ADR 0012: HTML only in v1, no JSON API

- **Status:** Accepted
- **Date:** 2026-10-20

## Context
[ADR 0003](0003-htmx-server-rendered-ui.md) chose a server-rendered UI. The open question was whether to also expose a JSON API for a mobile client or automation.

## Decision
The web app serves HTML only. There is no JSON API in v1. Routes keep their logic in services, so a JSON layer later is new routes, not a rewrite.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **HTML only (chosen)** | One interface to build, secure and test | No programmatic access |
| HTML plus JSON API | Mobile and automation ready | Two interfaces: double the auth, CSRF and test surface |

## Consequences
- Revisit after v1 if a mobile client or automation needs one.
