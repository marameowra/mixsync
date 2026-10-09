# `web`

## Purpose
The FastAPI app and the HTMX UI. A **composition root**: it wires adapters into services and exposes them as pages.

## Owns / does not own
- **Owns:** auth, sessions, CSRF, capability checks, routing, templates, static assets, SSE progress streams.
- **Does not own:** business logic. Routes call services and render results; nothing else.

## Proposed files
| Path | Contents |
|---|---|
| `deps.py` | FastAPI dependencies: DB session, current user, `require(capability)` |
| `auth.py` | Login/logout, argon2 (`argon2-cffi`) verification, session create/rotate, login rate limit |
| `csrf.py` | Per-session CSRF token; checked on every non-GET |
| `sse.py` | `sse-starlette` endpoints streaming HTML fragments for downloads/imports |
| [`routes/`](routes/README.md) | One module per page group |
| [`templates/`](templates/README.md) | Jinja2 templates |
| [`static/`](static/README.md) | CSS + vendored HTMX |

## Auth rules
- Real `<form method="post">` login with `autocomplete="username"` / `"current-password"` so password managers work.
- Sessions are server-side (`sessions` table). The cookie is `HttpOnly`, `Secure` (when served over HTTPS), and `SameSite=Lax`. The session id rotates on login.
- Lock out logins after 10 failures per 15 min per username+IP.
- CSRF: the token goes in a hidden form field; HTMX sends it as the `X-CSRF-Token` header via `hx-headers` on `<body>`.
- Every route declares its capability (`require(Capability.request)` etc.). There are no unguarded routes except `/login` and `/static`.

## HTMX conventions
- Full page on normal GET; a fragment when `HX-Request` is set. One template per page, fragments in `partials/`.
- Progress via SSE (`hx-ext="sse"`), never polling loops.
- Pages must work without JS for reading. Actions may require HTMX.

> [!question] JSON API
> Proposal: v1 serves HTML only and a JSON API is deferred, to avoid building two interfaces. Revisit if a mobile client or automation needs it.

## May import from
Anything (composition root).

## Tests
- Playwright: phone 375 px and desktop 1280 px; login with a real form submit; no horizontal scroll ([testing](../../../docs/testing.md#ui-tests)).
- Unit: every route rejects a user missing the capability; CSRF rejects missing/invalid tokens.

## Design docs
[ADR 0003](../../../docs/decisions/0003-htmx-server-rendered-ui.md) · [Architecture: users and permissions](../../../docs/architecture.md#users-and-permissions)
