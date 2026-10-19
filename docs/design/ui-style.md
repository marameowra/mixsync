# UI style and design direction

This brief guides the redesign of the whole MixSync interface. It sets the direction and records the settled decisions. The [server-rendered UI decision](../decisions/0003-htmx-server-rendered-ui.md) and [template](../../src/mixsync/web/templates/README.md) and [CSS](../../src/mixsync/web/static/README.md) rules remain in force.

## Agreed principles and rules

- **Calm Swiss utility.** Use clear typography, consistent alignment, a deliberate grid, and space to establish hierarchy. Keep decoration restrained so the content and next action lead.
- **Organize around tasks.** Group navigation by what people need to do: find music, resume work, and manage the library. Home should surface actionable work, especially items awaiting review, rather than act as a generic dashboard.
- **Let artwork help identify music.** Give covers prominence in discovery and release contexts. In review and import flows, evidence, consequences, and actions take priority over artwork size.
- **Make decisions legible.** On review and import screens, present release identity, matching evidence and confidence, file-operation consequences, and the next action in a clear reading order. Show uncertainty explicitly. Explain what will move, change tags, or enter trash before confirmation; retain the existing safety and undo paths.
- **Balance density.** Show essential facts without making task-heavy pages feel crowded. Keep supporting details easy to scan or reach. Use headings, alignment, and spacing before decorative containers.
- **Use precise language.** Name actions directly and describe their effects in plain words. Status, confidence, warnings, and errors must be understandable without color alone.
- **Design for every supported context.** Give light and dark modes equal care. Preserve semantic HTML, visible keyboard focus, labeled controls, reduced motion, and layouts that reflow without page-level horizontal scrolling. Keep tabular overflow within its container.
- **Reuse during implementation.** Extend existing Jinja templates, macros, and CSS patterns where they fit. Create a shared component for a genuinely repeated pattern; keep one-off layouts local to their page. Follow the existing server-rendered approach rather than introducing a new UI stack.

## Decisions

These choices are settled. They come from the MixSync design system. Check them on home, discovery, match review, and import at phone and desktop widths in both modes when production templates change.

### Palette

Warm-grey neutrals carry the interface. One cobalt accent marks links, the single primary action per view, the current nav item, and the focus ring. Text contrast is at least 4.5:1 and control borders and focus are at least 3:1, in both modes. The positive status is blue-teal, off the red–green axis, so it is not told from danger by hue.

| Token | Light | Dark | Use |
|---|---|---|---|
| `canvas` | `#f6f6f3` | `#131312` | Page background |
| `surface` | `#ffffff` | `#1b1b1a` | Tables, fields, action bar |
| `surface-sunken` | `#ecece8` | `#0d0d0c` | Table headers, missing-art tiles, bar troughs |
| `line` | `#d6d6d0` | `#31312e` | Hairline rules |
| `line-strong` | `#85857e` | `#75756e` | Input and secondary button borders |
| `ink` | `#191918` | `#ecece8` | Body text and headings |
| `ink-muted` | `#595954` | `#a6a69f` | Metadata, captions, table headers |
| `accent` | `#2343c4` | `#8da2ff` | Links, primary action, current nav, focus |
| `accent-strong` | `#1a3399` | `#b3c0ff` | Hover and pressed |
| `accent-tint` | `#e8ecfb` | `#1d2442` | Current nav item, selected row |
| `on-accent` | `#ffffff` | `#0e1530` | Text on accent fills |
| `focus` | `accent` | `accent` | 2px outline, 2px offset |
| `positive` / `-tint` | `#0d6475` / `#e2f1f4` | `#5cc4d6` / `#10292f` | Confident, done, verified |
| `caution` / `-tint` | `#8a5000` / `#fbf0dc` | `#f0b44c` / `#33260c` | Needs review, warnings |
| `danger` / `-tint` | `#b42318` / `#fbe9e7` | `#ff8f84` / `#3a1714` | Errors, rejected, destructive |
| `on-danger` | `#ffffff` | `#2a0a07` | Text on a filled danger button |

### Type

Use the system font stack only. Numbers, distances, sizes, durations, paths, and short MBIDs use the monospace stack with tabular figures.

| Style | Size / line | Weight | Use |
|---|---|---|---|
| `display` | 32 / 36 px | 650 | Page title (h1), one per page |
| `title` | 22 / 28 px | 600 | Section heading (h2) |
| `heading` | 17 / 24 px | 600 | Subsection (h3) |
| `body` | 15 / 22 px | 400 | Default text, cells, buttons, inputs |
| `body-strong` | 15 / 22 px | 600 | Counts and consequences inside a sentence |
| `small` | 13 / 18 px | 400 | Metadata, captions, help text |
| `label` | 12 / 16 px | 600 | Table headers, nav group names; uppercase via CSS |
| `data` | 13 / 18 px | 400 mono | Distances, bitrates, durations, paths |

### Spacing

The base is 4px.

| Token | Value | Use |
|---|---|---|
| `space-1` | 4px | Glyph to word in a status tag |
| `space-2` | 8px | Cell padding (vertical), button gap |
| `space-3` | 12px | Cell padding (horizontal), input padding |
| `space-4` | 16px | Phone gutter, paragraph spacing |
| `space-5` | 24px | Grid gutter, heading to its content |
| `space-6` | 32px | Gutter from 48rem, between subsections |
| `space-7` | 48px | Between top-level sections |
| `space-8` | 64px | Above the page title, around empty states |

### Grid

The page is 12 columns from one breakpoint at 48rem. Phones get one column in reading order. The page is at most 72rem wide and centred. Running prose is at most 38rem wide. The gutter between columns is `space-5`.

### Shape and depth

| Token | Value | Use |
|---|---|---|
| `radius-none` | 0 | Tables, rules, bars, nav |
| `radius-sm` | 2px | Buttons, inputs, tags |
| `radius-md` | 4px | Artwork, notices |

There are no shadows. Depth comes from `surface` on `canvas` and from hairlines. A 2px `ink` rule on top marks a key summary: the consequence summary and the home work queue.

### Navigation

| Group | Links | Shown when |
|---|---|---|
| Find | Discover, Watchlist, Requests | Discover and Watchlist need `request` |
| Resume | Review, Unverified | Unverified needs `library.edit` |
| Library | Fragments, Migration | Migration needs `admin` |

- The brand "MixSync" links home. Account and Log out sit at the end.
- Review and Unverified show a count of waiting items when it is above zero.
- A group is hidden when the person's capabilities hide all its links.
- The current page is marked with `aria-current="page"`.
- Nav wraps instead of collapsing into a menu.

### Artwork

| Token | Size | Use |
|---|---|---|
| `art-xs` | 40px | Table rows |
| `art-sm` | 64px | Review and plan headers |
| `art-md` | 120px | Release detail and follow on phones |
| `art-lg` | 176px | Discovery and release detail from 48rem |

A missing cover is a `surface-sunken` square with a hairline border and the title's first letter in `ink-muted`. It is labelled "No cover art for …".

### Review on narrow screens

The order is: action bar (sticky), score breakdown, candidate (cover and tracklist), source files, audio preview, full evidence tables, then other actions. Nothing is hidden behind a toggle. Tables scroll inside their own container.

### Status, warnings, errors, and destructive actions

- Every status shows a glyph, a word, and a colour. Colour is never the only signal.
- Confident is `positive` with ●. Needs review is `caution` with ◐. Rejected is `danger` with ✕. Neutral or pending is `ink-muted` with ○.
- Notices use the same glyphs. Warnings use ▲ and info uses i. A notice has no side stripe.
- Destructive actions use `danger` buttons. The button is filled only for the final confirm.
- The final confirm comes only after a consequence summary. The summary shows counts of what moves, changes, and goes to trash, and, above the data-safety threshold, asks the person to type the count.

### Shared and local patterns

- Shared now: data table wrapper, status tag, button, field, notice, and consequence summary.
- Shared once a second page uses it: artwork and audio preview.
- Kept local: the home work queue and the review score breakdown.

The decisions are settled and implemented in the production templates and CSS. The [`swiss` preview theme](../../design/themes/swiss/) remains as a reference prototype.
