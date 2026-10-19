# MixSync design system

The design system behind the UI, copied from <https://claude.ai/artifact/7TKmFQqUvNGyfEyq4DTAUq>. `tokens.json` holds every token; `components/bundle.css` is the component stylesheet; `components/<Name>/` has each component's guidelines and a static preview. Previews are fragments: they expect the tokens as CSS custom properties and `components/bundle.css` loaded around them, as the artifact page does. Settled decisions live in [`docs/design/ui-style.md`](../../docs/design/ui-style.md); `decisions.md` is the earlier proposal.

MixSync is a self-hosted tool that downloads music, checks it against MusicBrainz, and files it into a library. The interface uses a calm Swiss utility style: system type, a strict grid, hairline rules, one accent colour. Every screen leads with the task, the evidence and the next action. Pages are server-rendered Jinja with HTMX. There is no CSS framework, no web fonts and no runtime fetches from other origins.

## Content fundamentals

- **Name the action and its effect.** Buttons are a verb plus an object: "Accept match", "Pick another release", "Confirm merge: move 12, trash 2", "Re-check 14 pending reviews". Never "OK", "Submit", "Yes".
- **State consequences before they happen.** For example: "Originals go to .trash and can be restored with Undo." Or: "Imports the files with the tags below and no MusicBrainz ids." Say what will move, which tags change, and what goes to trash.
- **Say when something is uncertain.** Write "Needs review", "Partial match" or "No match". A distance always appears with its number (`0.142`). Never imply confidence you don't have.
- **Errors give the cause, then the fix.** "Disk full on /music. Free space, then Resume merge. Nothing was trashed."
- **Plain, second person, sentence case.** Use "your account", not "the user's account". Headings are sentence case. Uppercase appears only in the `label` style, applied by CSS.
- **Counts are real and pluralised.** "1 item awaiting review", "14 items awaiting review". Empty states say what was checked: "Nothing to review."
- **No emoji, no exclamation marks, no filler.** Internal values never reach the screen: `auto_accept` reads "Confident", `pending_review` reads "Needs review".

## Visual foundations

### Colour
- Neutrals carry the interface. The ground is `canvas`. Tables, inputs and the action bar sit on `surface`. Use `surface-sunken` for table headers, artwork fallbacks and bar troughs.
- Text is `ink`. Metadata, captions and table headers are `ink-muted`. Both pass 4.5:1 on every neutral ground in both themes.
- `accent` (cobalt) is the only hue that isn't a status. Use it for links, the single `primary` button per view, the current nav item (`accent-tint` ground) and the `focus` ring. On accent fills, set text in `on-accent`; it turns dark in dark mode.
- Status hues are `positive` (blue-teal), `caution` (amber) and `danger` (red), each with a `-tint` ground. `positive` sits off the red–green axis on purpose. A status never relies on hue: it always comes with a glyph (● ◐ ✕ ○ ▲) and a word.
- `danger` also marks destructive actions (trash, discard, reject) and the Trash count.
- Hairlines are `line`. Control borders are `line-strong` (3:1). Don't box content just to separate it. Use space and a rule.
- Light and dark get equal care. Every token has both values. The app follows `prefers-color-scheme`.

### Type
- System font stack only (`sans`). Numbers, distances, sizes, durations, paths and short MBIDs use `mono` with tabular figures.
- Hierarchy: `display` for the h1 (one per page), `title` for h2, `heading` for h3, `body` as the default, `body-strong` for counts and consequences inside a sentence, `small` for metadata and help, `data` for numbers, `label` for table headers and nav group names.
- Keep running prose within `measure` (38rem).

### Space and grid
- 4px base: `space-1` to `space-8`. Separate sections with `space-7`, subsections with `space-6`, and a heading from its content with `space-5`.
- `main` is centred at `content-max` (72rem). The page gutter is `space-4` on phones and `space-6` from the single `breakpoint` (48rem) up.
- From 48rem up, use 12 columns with a `space-5` gutter. Review detail splits 6/6: source on the left, candidate on the right. Evidence and actions span the full width below. Phones get one column, in reading order.
- Use headings, alignment and space before containers. A page has no cards by default.

### Shape, borders and depth
- `radius-none` for tables, rules and bars. `radius-sm` (2px) for buttons, inputs and tags. `radius-md` (4px) for artwork and notices. Nothing is rounder.
- No shadows. Depth comes from `surface` sitting on `canvas` and from hairlines. The sticky action bar uses a top and bottom `line`.
- A 2px `ink` top rule marks a key summary: the consequence summary and the home work queue.

### Artwork
- Covers help people recognise music. They are large (`art-lg`) in discovery and release views, small (`art-sm`) in review and plan headers, and `art-xs` in table rows.
- Missing cover: a `surface-sunken` square with a hairline border and the title's first letter in `ink-muted`, labelled "No cover art for …".

### State and motion
- Focus: a 2px solid `focus` outline with a 2px offset on every interactive element. Never remove it.
- Hover: darken to `accent-strong`, or strengthen a border to `ink`. Disabled: 50% opacity, with a reason in text nearby.
- Motion: colour and opacity only, 150 ms at most, all of it off under `prefers-reduced-motion: reduce`.

### Layout behaviour
- No page-level horizontal scroll at any width. Wide tables scroll inside `.table-wrap`.
- Nav, action bars and button rows wrap instead of collapsing into menus. Every page works without JS.

## Iconography

MixSync has no icon set. Status glyphs are Unicode geometric shapes drawn by CSS: ● confident/ok, ◐ needs review, ✕ rejected/error, ○ neutral/pending, ▲ warning, i info. Sort arrows are ↑ and ↓. Everything else is a word. Don't add an icon font or SVG sprite unless a pattern repeats and words fail. If you do, add it here first.

## Components

| Component | Where |
|---|---|
| AppNav | `base.html`: task-grouped nav (Find · Resume · Library) |
| WorkQueue | home: work waiting on the person |
| ActionBar | review detail, plan: sticky identity + verdict + actions |
| ConsequenceSummary | plan, any file operation: counts, sentence, typed confirm |
| ScoreBreakdown | review evidence: penalty per feature |
| DataTable | every list of records |
| StatusTag | bands and statuses |
| Notice | action results, warnings, errors |
| Field | every form control |
| Button | every action |
| Artwork | covers and their fallback |
| AudioPreview | review: listen before deciding |

`components/bundle.css` is the proposed `web/static/app.css`. Its classes map to the Jinja macros in `macros/forms.html` and the planned `macros/media.html`. Previews are static HTML. The app has no component JavaScript.
