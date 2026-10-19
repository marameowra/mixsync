# ActionBar
The sticky bar at the top of a decision page (review detail, plan) holding the identity, the verdict, and the primary actions.

## Content order
1. StatusTag with the band.
2. Release identity in `body-strong` (artist — title).
3. One line of the most decisive evidence in `ink-muted`: distance, files matched.
4. Actions on the right: one `primary`, then alternatives, destructive last.

When the page cannot offer the primary action, say why in the summary ("No MusicBrainz release found: pick one or mark the album unverified.") and omit the button rather than disabling it.

## The consumer provides
`role="region" aria-label="Decision"`, one `<form>` per action with CSRF. Wraps to two lines on phones; actions drop below the summary.
