# WorkQueue
Home's list of work waiting on the person: a big count, what it is, and the action that resumes it.

## Use
- Order by urgency: review, promotions, unverified, fragments, failed requests.
- A non-zero row gets an action; the first non-zero row's action is the page's one `primary`.
- Zero rows stay, muted, with "Nothing to …" so the person knows it was checked.
- Pluralise properly ("1 item", "14 items").

## The consumer provides
Counts from the home route and the links. Not a dashboard: no charts, no totals that need no action.
