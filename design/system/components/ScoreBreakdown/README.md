# ScoreBreakdown
Shows how much each matching feature added to the distance, so a reviewer sees why a match is uncertain.

## Content
One row per `breakdown` entry: feature name, a bar of its weighted penalty relative to the reject threshold, the number in mono, and the `reason` in `small` `ink-muted` when present. Bars over half the threshold get `.high` (danger fill); the number and reason carry the meaning, not the colour.

## Use
At the top of the Evidence section on review detail, before vetoes, AcoustID, and duration deltas. The full table (penalty, weight) stays available below it as a DataTable.

## The consumer provides
`d.breakdown`, the threshold for scaling, and the bar width. The preview sets width inline for illustration; in the app use a `<meter>` or a width class so CSP stays `default-src 'self'` (no inline styles).
