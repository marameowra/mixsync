# UI style and design direction

This brief guides a future redesign of the whole MixSync interface. It sets direction, not a finished visual specification. The [server-rendered UI decision](../decisions/0003-htmx-server-rendered-ui.md) and [template](../../src/mixsync/web/templates/README.md) and [CSS](../../src/mixsync/web/static/README.md) rules remain in force.

## Agreed principles and rules

- **Calm Swiss utility.** Use clear typography, consistent alignment, a deliberate grid, and space to establish hierarchy. Keep decoration restrained so the content and next action lead.
- **Organize around tasks.** Group navigation by what people need to do: find music, resume work, and manage the library. Home should surface actionable work, especially items awaiting review, rather than act as a generic dashboard.
- **Let artwork help identify music.** Give covers prominence in discovery and release contexts. In review and import flows, evidence, consequences, and actions take priority over artwork size.
- **Make decisions legible.** On review and import screens, present release identity, matching evidence and confidence, file-operation consequences, and the next action in a clear reading order. Show uncertainty explicitly. Explain what will move, change tags, or enter trash before confirmation; retain the existing safety and undo paths.
- **Balance density.** Show essential facts without making task-heavy pages feel crowded. Keep supporting details easy to scan or reach. Use headings, alignment, and spacing before decorative containers.
- **Use precise language.** Name actions directly and describe their effects in plain words. Status, confidence, warnings, and errors must be understandable without color alone.
- **Design for every supported context.** Give light and dark modes equal care. Preserve semantic HTML, visible keyboard focus, labeled controls, reduced motion, and layouts that reflow without page-level horizontal scrolling. Keep tabular overflow within its container.
- **Reuse during implementation.** Extend existing Jinja templates, macros, and CSS patterns where they fit. Create a shared component for a genuinely repeated pattern; keep one-off layouts local to their page. Follow the existing server-rendered approach rather than introducing a new UI stack.

## Open decisions

These choices are intentionally unresolved. Decide them against the principles above and check them on home, discovery, match review, and import at phone and desktop widths in both appearance modes.

| Decision to make | What should guide it |
|---|---|
| Which neutral palette and single accent hue should light and dark modes use? | Readability, contrast, and a calm tone; check real status and focus states. |
| What exact type, spacing, and grid scales create the intended hierarchy? | Balanced density across short lists, tables, forms, and long review pages while retaining the system font stack. |
| Which task groups and labels should appear in navigation? | Current routes, user permissions, and how quickly people can find or resume work. |
| How large should artwork be in each context, and what appears when it is missing? | Music recognition in discovery and release views; space for evidence and actions in review. |
| How should review evidence, audio preview, and actions rearrange on narrow screens? | Keep the decision sequence clear without hiding critical evidence or causing page-level horizontal scroll. |
| What visual treatments should distinguish confidence, warnings, errors, and destructive actions? | Legibility in both modes, text alongside color, and clear consequences before file operations. |
| Which repeated patterns warrant shared templates or macros? | Reuse only after a pattern is present in multiple places and its behavior is genuinely the same. |

This document does not authorize UI changes or settle the open decisions. Mockups and implementation belong to later work.
