# Button
A native `<button>` for every action, in four weights: secondary (default), `primary`, `danger`, `quiet`.

## Use
- Exactly one `.btn.primary` per view: the next action the page exists for (Accept match, Confirm merge, Save).
- `.btn` (secondary) for the alternatives beside it.
- `.btn.danger` for anything that trashes, discards or rejects. `.btn.danger.filled` only for the final confirm of a file operation that sends files to `.trash`, and only after a Consequence summary.
- `.btn.quiet` for low-stakes, reversible side actions (Keep, Undo in a table row).

## Label
Verb + object, describing the effect: "Accept match", "Confirm merge: trash 2 files", "Re-check 14 pending reviews". Never "OK", "Submit" or "Yes".

## The consumer provides
The `<form method="post">` with `{{ forms.csrf() }}`, `type="submit"`, the label. Buttons work without JS; HTMX only enhances.

## Don't
- Don't put two primaries side by side.
- Don't use colour alone: a destructive label must say what is destroyed.
- Don't wrap links as buttons; navigation is `<a>`.
