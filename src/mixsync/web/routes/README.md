# `web/routes`

One module per page group. Each route: check the capability → call a service → render a template. No business logic here.

## Pages (v1)
| Module | Paths | Capability |
|---|---|---|
| `auth.py` | `GET/POST /login`, `POST /logout` | none |
| `home.py` | `/`: active requests, review count, expiring-soon count | `request` |
| `search.py` | `/search`: MusicBrainz search → request button | `request` |
| `requests.py` | `/requests`, `/requests/{id}`: status, candidates, download progress (SSE) | `request` |
| `review.py` | `/review`, `/review/{decision_id}`: side-by-side evidence, `<audio>` preview, Accept / Pick other / Mark unverified / Reject | `request` (own items); `approve` (anyone's) |
| `library.py` | `/library`, `/library/unverified`, `/library/fragmentation`, `/library/trash` | `request` to view; `library.edit` to act |
| `watchlist.py` | `/watchlist`, `/watchlist/{follow_id}` | `request` |
| `discover.py` | `/discover` (playlists + definitions), `/discover/expiring` | `request` |
| `migration.py` | `/migration`: preflight, progress, report | `admin` |
| `settings.py` | `/settings` (own profile, linked accounts, matching profile) | logged in |
| `admin.py` | `/admin/users`, `/admin/settings`, `/admin/jobs` | `admin` |
| `events.py` | `/events/*` SSE streams | same as the page it feeds |
| `media.py` | `/media/preview/{file_id}`: range-request audio for review previews | `request` |

## Rules
- Mutating routes are `POST` only, CSRF-checked, and return a fragment (HTMX) or a redirect (no-JS).
- Bulk actions (merge, purge, reorganize) are two steps: `POST …/plan` renders the dry-run, then `POST …/execute` with the plan id and the typed count.
- `media.py` serves files only from `/downloads` and `/data`, resolved and checked against those roots (no path traversal).

## Design docs
[Matching: review screen](../../../../docs/design/matching.md#review-screen) · [Data safety: dry-run](../../../../docs/design/data-safety.md#5-dry-run-and-confirm)
