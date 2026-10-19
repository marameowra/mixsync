# Notice
A short message about the result of an action or a condition on the page: glyph, a bold headline, one sentence of detail.

## Tones
`info` (i, accent), `ok` (●, positive), `warn` (▲, caution), `error` (✕, danger), each on its `-tint`. The headline states the fact; the sentence says what happens next or what to do.

## Use
- `role="status"` for results of the last action, `role="alert"` for errors that block progress.
- Place it directly under the page title, or directly above the form it concerns.
- Error copy: cause, then fix. "Disk full on /music. Free space, then Resume merge."

## Don't
No coloured side stripe, no dismiss button on errors, no stacking more than two notices.
