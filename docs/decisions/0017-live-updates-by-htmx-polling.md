# ADR 0017: Live updates by HTMX polling, not SSE

- **Status:** Accepted
- **Date:** 2026-10-20
- **Supersedes:** the SSE consequence of [ADR 0003](0003-htmx-server-rendered-ui.md). The rest of 0003 stands.
- **Note:** planned, not built (#221).

## Context
[ADR 0003](0003-htmx-server-rendered-ui.md) chose server-rendered pages with HTMX, and live progress (downloads, imports) through server-sent events (SSE). Neither was built: pages with changing state say "Reload to see the result".

The web app and the worker can run as separate processes (`mixsync web`, `mixsync worker`) that share state only through the database ([ADR 0004](0004-sqlite-and-db-job-queue.md)). SQLite has no change notification. An SSE endpoint would therefore poll the database itself and push what it finds, so it is no fresher than the browser polling, and it holds one long-lived connection per open tab.

## Decision
- A **live region** is a server-rendered fragment that HTMX re-fetches every 5 s while the tab is visible (`hx-trigger="every 5s [!document.hidden]"`) and swaps in place.
- The server ends the polling: once nothing in the region is in progress, it renders the fragment without the trigger.
- A route answers an HTMX request (`HX-Request` header) with only the fragment, and a normal request with the full page.
- htmx is vendored into the app's static files, pinned, with its license. No CDN.
- Every page works with JavaScript off: forms are real POSTs, and a live region shows its state as of page load.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **HTMX polling (chosen)** | No new server machinery; works across processes through the DB; stops itself; no-JS fallback is the page itself | A request every 5 s per visible tab with live work; up to 5 s stale |
| SSE (ADR 0003's plan) | Push model; one connection | Still polls the DB behind the stream; a held connection per tab; reconnect handling; proxies may buffer it |
| Postgres `LISTEN/NOTIFY` push | Real push | Postgres only; SQLite, the default, has nothing like it |

## Consequences
- Each live region is a fragment template plus a route that can return it alone; regions are added one at a time.
- Load scales with visible tabs showing live work, not with open tabs.
- If true push is needed later (e.g. second-by-second download progress), revisit with this trade-off; the fragment routes stay usable by SSE.
