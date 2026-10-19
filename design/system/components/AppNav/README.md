# AppNav
The top navigation in `base.html`, grouped by task: **Find** music, **Resume** work, manage the **Library**.

## Groups
| Group | Links | Shown when |
|---|---|---|
| Find | Discover, Watchlist, Requests | Discover and Watchlist need `request` |
| Resume | Review, Unverified | Unverified needs `library.edit` |
| Library | Fragments, Migration | Migration needs `admin` |
| (end) | Account, Log out | always |

Hide a group whose links are all hidden. Resume links carry a count (`.count`, mono) of items waiting; omit the count when it is zero.

## The consumer provides
`<nav aria-label="Main">`, `aria-current="page"` on the current link, the counts from the home queries, capability checks as today.

## Behaviour
Wraps onto more lines at narrow widths; no hamburger menu, no JS. The group name is a visual label only (`.group-name`), links stay plain `<a>`.
