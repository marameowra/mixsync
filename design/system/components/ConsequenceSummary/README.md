# ConsequenceSummary
States what a file operation will do, in counts and a sentence, before the confirm button. Required before any action that moves, retags or trashes files.

## Content
- Three counts: Move, Trash, Keep (`danger` on Trash when above zero). Hide a zero count's emphasis, never the count.
- One plain sentence: where files go, what tags change, where originals end up, how to undo.
- The confirm: `forms.plan_action`. Above the data-safety threshold it asks the person to type the change count ("Type 14 to confirm"); the button label repeats the effect.

## The consumer provides
`counts` and the changed-op total from the plan, `typed_above` from settings, the existing `/library/plans/<id>/<action>` form.

## Don't
Never put a destructive confirm before this summary on the page. Don't shorten "can be restored with Undo" away: the undo path is part of the decision.
