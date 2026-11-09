# ADR 0023: Svelte single-page app over an internal JSON API

- **Status:** Accepted
- **Date:** 2026-11-09
- **Supersedes:** [ADR 0003](0003-htmx-server-rendered-ui.md) (server-rendered UI, island clause), [ADR 0012](0012-html-only-ui-in-v1.md) (HTML only, no JSON API) and [ADR 0020](0020-htmx-is-the-only-js.md) (htmx is the only JS), for each screen once it moves. Until then they apply to the htmx pages that remain.
- **Amends:** [ADR 0017](0017-live-updates-by-htmx-polling.md): polling stays, done by the client with `fetch`.
- **Epic:** #377

## Context
Review is the screen people spend the most time on, and every decision waits for a full page round trip before the next one shows. The review and library screens need richer tables than server-rendered HTML gives cheaply: client-side sort and filter, column choice, expandable rows. Under ADR 0020, keyboard shortcuts fell back to `accesskey` modifiers and clipboard copy wasn't possible.

## Decision
**Shape**
- Svelte with SvelteKit's static adapter in SPA mode, in TypeScript, in `frontend/`. npm with a committed lockfile; Node pinned in `.nvmrc`. No server-side rendering and no Node at runtime: a stage in `deploy/Dockerfile` builds the files, and the Python app serves them.
- The SPA lives at `/app/*` for good; `/` redirects to it. Screens move one at a time: review, then the library, then the rest. When a screen moves, its old GET URLs answer 308 to the `/app` URL, and its HTML routes and templates are deleted.
- During the migration the htmx pages keep their own nav; links between the two are full page loads.

**API**
- JSON routes under `/api`, internal to the SPA: no versioning, no tokens, no stability promise. Pydantic models; TypeScript types generated from the OpenAPI schema; errors use FastAPI's `{"detail": ...}`.
- Routes call the existing services. No business logic in the client: it calls `/api` and renders.
- `GET /api/session` returns the user, capabilities and CSRF token, or 401, on which the SPA goes to `/login?next=<path>`. Writes send `X-CSRF-Token` (already accepted by `web/csrf.py`).
- Login stays a server-rendered `<form>` POST, for password managers.

**Behaviour**
- No "works with JavaScript off" rule for SPA screens.
- Live data: the client polls every 5 s with `fetch` and stops once nothing is in progress (ADR 0017's reasoning: SQLite has no change notification).
- Review queue: the next 1 to 2 decisions are prefetched, no more, because a review's names resolve through MusicBrainz at about 1 request per second.
- Accept and Reject advance optimistically. A refusal (409, e.g. already settled by another approver, files missing) or a network error shows a banner that stays until dismissed and links back to the decision, which stays pending. Nothing is retried automatically. The first action on a decision wins.
- Review keys: `a` accept, `r` reject, `j`/`k` next/previous, `space` play/pause preview, `?` key list; ignored while typing. Accept and Reject act without a prompt. Repick, Split and Mark unverified stay mouse-driven.
- The library sends every album in one response and filters on the client; an album's tracks load when its row expands. Revisit at the `albums.search` shortcut's ~100k.
- Tables use a house `DataTable` component (sort, text filter, column show/hide, expandable rows). Add virtualization only past ~10k rows.

**Rules kept**
- Safety gates stay on the server: a destructive flow is a plan call that returns an id, then a confirm call that takes it ([data safety](../design/data-safety.md#5-dry-run-and-confirm)). Each such route has a test that confirm is refused without a valid plan. Typed confirmations are checked on the server.
- CSP stays strict: `script-src 'self'`, no `'unsafe-eval'`, no `'unsafe-inline'`. SvelteKit's inline bootstrap script is allowed by its `sha256` hash, computed by the server at startup from the built `index.html`, for `/app/*` only.
- Accessibility: focus moves to the main heading on route change; banners announce through an `aria-live` region; 4.5:1 text contrast in light and dark; `prefers-reduced-motion` respected.
- Tests of a moved screen's HTML routes are rewritten in place as `/api` tests of the same behaviour, not deleted. If a whole file becomes pointless, ask first (AGENTS.md).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Svelte SPA + internal JSON API (chosen)** | Instant review flow; rich tables everywhere; one interaction style once migrated | Node toolchain in CI and the image; two codebases; larger API, CSRF and test surface |
| Islands on review and library (amend ADR 0020) | No build step; keeps CSP, JS-off and HTML-only; a few days of work | Two interaction styles for good; becomes a hidden SPA once islands spread |
| htmx with `preload` and `hx-boost` | No new decisions | Faster but still waits; no rich tables |
| Plain Svelte + Vite instead of SvelteKit | No inline script, so no CSP hash | Hand-rolled routing and data loading across ~30 screens |
| SSE for live data | Push model | Still polls the DB behind the stream (ADR 0017) |
| Versioned public API (`/api/v1`) | Ready for mobile and automation | A stability promise nobody needs yet; tokens and docs to maintain |

## Consequences
- `scripts/check.sh` runs `npm ci`, `svelte-check`, ESLint and Prettier (also in `--fast`), and Vitest in full runs; frontend steps are skipped when nothing under `frontend/` changed. Playwright (#300) runs as its own CI job.
- AGENTS.md documents `frontend/`, its dev setup and checks, and the no-logic-in-the-client rule.
- The redesign issues #305, #322 and #323 are superseded by this epic's sub-issues; their designs carry over without their htmx constraints.
- A public API for mobile or automation remains out of scope; ADR 0012's "revisit" now points here.
