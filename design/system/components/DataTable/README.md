# DataTable
Hairline tables for every list of records: reviews, files, tracks, plan operations, evidence.

## Use
- Wrap every table in `.table-wrap` so wide tables scroll inside their container, never the page.
- Headers `scope="col"`, styled as `label` (uppercase via CSS) in `ink-muted` on `surface-sunken`.
- Numbers (distance, bitrate, size, length) get `class="n"` on both `th` and `td`: mono, tabular, right-aligned.
- IDs and timestamps use `.num` (mono, left-aligned). File paths use `<span class="path">`.
- Sortable columns: the header text is a link; the active one has `aria-sort="ascending|descending"` and shows an arrow.
- Selected row (keyboard or bulk action): `aria-selected="true"` → `accent-tint`.

## The consumer provides
`<table>` markup with `<thead>`/`<tbody>`, the sort links (server-side, as today), an empty state paragraph instead of an empty table ("Nothing to review.").

## Don't
No zebra stripes, no cell borders, no card per row. Don't hide columns on phones; let the wrapper scroll.
