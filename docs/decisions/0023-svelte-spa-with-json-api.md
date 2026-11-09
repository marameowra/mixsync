# ADR 0023: Svelte single-page app over a JSON API

- **Status:** Proposed (to be grilled on the epic before any work starts)
- **Date:** 2026-11-09
- **Would supersede:** [ADR 0003](0003-htmx-server-rendered-ui.md)'s server-rendered UI and island clause, [ADR 0012](0012-html-only-ui-in-v1.md) (HTML only, no JSON API), [ADR 0020](0020-htmx-is-the-only-js.md) (htmx is the only JS). [ADR 0017](0017-live-updates-by-htmx-polling.md)'s polling model is open below.

## Context
Review is the screen people spend the most time on, and every decision waits for a full server round trip before the next one shows. The review and library screens also need richer tables than server-rendered HTML gives cheaply: client-side sort and filter, column choice, expandable rows. Under ADR 0020 keyboard shortcuts fall back to `accesskey` modifiers and clipboard copy is not possible.

Two routes were compared: islands (a vendored JS module on review and library only, amending ADR 0020) and a single-page app. Islands cost less, but leave two interaction styles in one UI and turn into an SPA without its tooling once more screens need them. The maintainer chose the SPA.

## Decision (proposed)
- **Frontend:** Svelte with SvelteKit's static adapter in SPA mode, built to static files served by the FastAPI app from the same origin. No server-side rendering in Node; the Python app stays the only server.
- **API:** JSON routes under `/api/v1`, with Pydantic request and response models. The routes call the same services the HTML routes call today; no logic moves into the client. TypeScript types are generated from the OpenAPI schema.
- **Migration:** screen by screen. Review and the library come first; htmx pages stay until their screen is moved, then their HTML routes are deleted.
- **Login** stays a real server-rendered `<form>` POST so password managers keep working (ADR 0003's requirement).
- **Auth and CSRF:** the existing session cookie. Unsafe API methods need a CSRF token in a request header.
- **Safety gates stay on the server.** Dry-run plans and confirmations are API steps (`plan` then `confirm`), never client-only prompts ([data safety](../design/data-safety.md#5-dry-run-and-confirm)).
- **CSP stays strict:** `script-src 'self'`, no `'unsafe-eval'`, no `'unsafe-inline'`. SvelteKit's inline bootstrap script is allowed by hash (`kit.csp`), not by `'unsafe-inline'`.
- **Review prefetch** is shallow (the next 1 to 2 decisions), because a review's names resolve through MusicBrainz at about 1 request per second ([services etiquette](../design/services-etiquette.md)).

## Open questions for the grilling session
1. JS-off support: dropped for migrated screens, or kept for some flows?
2. Live updates: keep 5 s polling (now `fetch`), or move to SSE?
3. Library size: send everything for client-side filtering, or keep server paging with client sort inside a page? Decide from a real library's album count.
4. Table component: a library (which one, checked against the CSP) or a small house component?
5. Do #305, #322 and #323 (redesign) move into this epic, or ship on htmx first?
6. Tests and checks: Vitest and Playwright (#300) in `scripts/check.sh` and CI; how `--fast` and the implementer Stop hook cover frontend changes.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Svelte SPA + JSON API (proposed)** | Instant review flow; rich tables everywhere; one interaction style; the API also serves future mobile or automation clients | Node toolchain in CI and the image; two codebases; API, CSRF and test surface roughly doubles; reverses three ADRs |
| Islands on review and library (amend ADR 0020) | No build step; keeps CSP, JS-off and HTML-only; a few days of work | Two interaction styles; hidden SPA if islands spread past 3 to 4 screens |
| htmx with `preload` and `hx-boost` | No new decisions | Faster but still waits; no rich tables |

## Consequences
- `deploy/Dockerfile` gains a Node build stage; the runtime image stays Python-only and serves the built files.
- `scripts/check.sh` and `.github/workflows/ci.yml` gain frontend lint, type-check and test steps.
- Every safety gate needs an API test that the confirm step can't be skipped.
- Implementer and reviewer briefs (AGENTS.md) need frontend checks and file locations.
- ADRs 0003, 0012 and 0020 are marked superseded once this is accepted; ADR 0017 depends on question 2.
