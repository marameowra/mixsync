# swiss

The MixSync design system as a preview theme: calm Swiss utility style, warm-grey neutrals, one cobalt accent, light and dark from `prefers-color-scheme`. Design system: [`design/system/`](../../system/) (published at <https://claude.ai/artifact/7TKmFQqUvNGyfEyq4DTAUq>). The decisions are recorded in [`docs/design/ui-style.md`](../../../docs/design/ui-style.md).

## View
`uv run python -m devtools.preview`, then open `/__preview?theme=swiss`.

## What it overrides
- `static/theme.css`: tokens as CSS custom properties, the component CSS, and fallback styling for the real templates' bare elements (`button`, inputs, tables, `.scroll`, `.cols`, `.cover`, `.actionbar`, `.band`, `[role=alert]`, `[role=status]`). It replaces `app.css`.
- `templates/base.html`: the grouped nav (Find, Resume, Library) with the same capability checks as the real nav.
- `templates/pages/home.html`: the "Waiting on you" work queue, built only from the home route's context.

## Not yet covered
- Every other page still uses the real templates, restyled by the fallback rules only. The review action bar, consequence summary, score breakdown, artwork macro and audio preview have CSS but no templates.
- Home has no "In progress", "New in Discover" or "Since you were last here" sections. They need data the route does not provide.
