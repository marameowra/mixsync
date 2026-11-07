# ADR 0020: htmx is the only JavaScript, used for UX on any page

- **Status:** Accepted
- **Date:** 2026-11-06
- **Extends:** [ADR 0017](0017-live-updates-by-htmx-polling.md), which used htmx only for live regions. The rest of 0017 stands, including its rule that every page works with JavaScript off. [ADR 0003](0003-htmx-server-rendered-ui.md)'s island clause stands.

## Context
Pages are plain full-page renders except for live regions (ADR 0017). Planned UX work needs more than that: review shortcuts (#322), a collapsible sidebar (#305), and actions that don't reload the whole page. The open question was how much JavaScript to allow without drifting toward a client-side app. A size budget for hand-written JS was considered and rejected: it limits bytes, not what the code does.

## Decision
- **htmx is the only script.** It stays vendored and pinned (ADR 0017). There is no hand-written JS, no inline `<script>`, no `on*=` attributes and no `hx-on` (also inline script). Enhancements are `hx-*` attributes on server-rendered HTML.
- **Pages work with JavaScript off.** htmx adds behaviour to a page that already works: forms are real POSTs and links are real links. A feature can exist only with JS on (a keyboard shortcut, an inline swap) if the same task can still be done without JS.
- **The server owns state.** A UI preference (e.g. the sidebar's collapsed state) is saved by an htmx POST to the session or user row, not in `localStorage`. The page renders it on the next load with or without JS.
- **The server returns HTML only.** htmx requests get fragments, like live regions do, so this needs no JSON API ([ADR 0012](0012-html-only-ui-in-v1.md)).
- **Safety gates stay on the server.** htmx can make a dry-run plan or a confirmation step easier to use, but it never replaces one ([data safety](../design/data-safety.md#5-dry-run-and-confirm)). A JavaScript `confirm()` prompt is not a safety gate.
- **Escape hatch:** anything htmx cannot do (e.g. copy to clipboard) goes without, or uses ADR 0003's island clause in its own ADR.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **htmx only (chosen)** | Keeps the code small without a byte count; all behaviour is in templates and routes; state is in one place | Some conveniences round-trip to the server; a few can't be done at all |
| One hand-written `app.js` with a size budget | Covers clipboard, client-side select-all, `localStorage` | A byte count doesn't stop client state or client rendering; a second place for behaviour |
| Alpine.js on every page | Declarative client-side state | A second framework; pulls state into the browser; 0003 keeps it for one island only |

## Consequences
- Every new interactive feature is a route that returns a fragment, plus `hx-*` attributes. Route tests cover it with `HX-Request`, as live regions are tested.
- htmx trigger filters (`[...]`, e.g. ADR 0017's `[!document.hidden]`) are evaluated with `eval`, so a strict Content Security Policy would need `'unsafe-eval'` or would have to drop filters (#330).
- Six handlers in existing templates break this rule. They are to be converted (#329), along with a template test that blocks new inline scripts: five `onsubmit="return confirm(...)"` handlers (`review_list.html` 1, `watchlist_follow.html` 1, `users.html` 3) and the select-all `onclick` in `watchlist_catalog.html`. Swapping in `hx-boost` + `hx-confirm` needs care: htmx 2 doesn't swap 4xx responses by default, so a rejected form would fail silently.
- Preview themes under `design/themes/` are dev-only and may use their own JS; a theme's templates follow this ADR once they move into `src/mixsync/web`.
