# `web`

## Purpose
The FastAPI app and server-rendered UI. A composition root: it exposes services as pages. HTML only ([ADR 0012](../../../docs/decisions/0012-html-only-ui-in-v1.md)).

## Owns / does not own
- **Owns:** routing (`routes/`, one module per page group), sessions and login (`routes/auth.py`), CSRF (`csrf.py`), capability checks (`deps.py`, `require(Capability.x)`), Jinja2 templates (`templates/`: `base.html`, `pages/`, `macros/`), and the hand-written stylesheet (`static/app.css`, served at `/static`).
- **Does not own:** business logic. A route checks the capability, calls a service, renders a template.

## Rules / invariants
- Every route declares its capability; the only unguarded ones are login, `/healthz` and `/static`.
- Sessions are server-side. The cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS; logins lock out after 10 failures.
- Every non-GET is CSRF-checked: the token is a hidden form field (`forms.csrf()`) or an `X-CSRF-Token` header.
- Mutating routes are POST only and redirect. Bulk actions (merge today; early trash purge #293 and reorganize #285 are planned) are two steps: render the dry-run plan, then confirm it (typed count above 50 ops) ([data safety](../../../docs/design/data-safety.md#5-dry-run-and-confirm)).
- `media.py` serves audio and cover art only from resolved paths under the downloads, data, import-source and drop roots (no traversal).
- Templates: autoescape on, never `|safe` on user or API data; no inline styles or scripts, so a strict CSP stays possible; every page works without JS. Tokens and components: [design/system/README.md](../../../design/system/README.md).
- Assets are served from `'self'` only: no CDN, no web fonts, no build step.
- Nothing under `web` calls MusicBrainz on list pages; `names.py` resolves ids from local rows and the cache first.

HTMX live regions (the `live` macro) poll for progress on the requests and migration pages; there are no SSE streams ([ADR 0017](../../../docs/decisions/0017-live-updates-by-htmx-polling.md)). Other pages are plain full-page renders.

UI preview (dev only): see [devtools/preview](../../../devtools/preview/).

## May import
Anything (composition root).

## Design doc
[ADR 0003](../../../docs/decisions/0003-htmx-server-rendered-ui.md) · [Matching: review screen](../../../docs/design/matching.md#review-screen) · [Architecture: users and permissions](../../../docs/architecture.md#users-and-permissions)
