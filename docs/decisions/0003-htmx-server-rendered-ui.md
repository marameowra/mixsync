# ADR 0003: Server-rendered UI with HTMX

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
UI requirements:
- Responsive pages that reflow well on any platform.
- Visually simple, restrained design with no flashy animation.
- A login that works with password managers. soulsync's login does not.
- The main interactive screen is match review: side-by-side evidence plus an audio preview.

## Decision
**Server-rendered Jinja2 templates + HTMX + server-sent events (SSE)**:
- one hand-written CSS file (no CSS framework)
- native `<audio>` for previews
- the login is a real `<form>` POST with correct `autocomplete` attributes

If the match-review screen outgrows HTMX, it gets **one small island component** (an Alpine or Svelte widget). The app does not become a single-page app (SPA).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **HTMX + server templates (chosen)** | Restrained by construction; little JS; one codebase; no build step; real forms; reflows with plain CSS grid/flex | Rich interactions take more care; less of a component ecosystem |
| SvelteKit SPA (static adapter) | Great for interactive review and live progress; small bundles | A second codebase and build pipeline; auth forms need deliberate care |
| React / Next.js | The biggest ecosystem | The heaviest option; pulls toward flashy design; overkill |

## Consequences
- Live progress (downloads, imports) uses SSE fragments swapped in by HTMX.
- Styling rules: system font stack, one accent color, CSS grid/flex only, `prefers-reduced-motion` respected, no transitions longer than 150 ms.
- Playwright tests cover the phone and desktop widths ([testing](../testing.md#ui-tests)).
