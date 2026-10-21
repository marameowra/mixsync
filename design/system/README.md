# MixSync design system

The design system behind the UI, copied from <https://claude.ai/artifact/7TKmFQqUvNGyfEyq4DTAUq>. `tokens.json` holds every token; `components/bundle.css` is the component stylesheet; `components/<Name>/preview.html` is each component's static preview. Previews are fragments: they expect the tokens as CSS custom properties and `components/bundle.css` loaded around them, as the artifact page does. This file is the canonical UI doc; [`docs/design/ui-style.md`](../../docs/design/ui-style.md) only points here. Production CSS is `src/mixsync/web/static/app.css`; see [web/README.md](../../src/mixsync/web/README.md) for templates and the theme preview.

MixSync is a self-hosted tool that downloads music, checks it against MusicBrainz, and files it into a library. The interface uses a calm Swiss utility style: system type, a strict grid, hairline rules, one accent colour. Every screen leads with the task, the evidence and the next action. Pages are server-rendered Jinja. There is no CSS framework, no web fonts and no runtime fetches from other origins.

> Planned — not built (#221): HTMX enhancement. Every page works without JS today.

## Principles

- **Organize around tasks.** Group navigation by what people do: find music, resume work, manage the library. Home surfaces actionable work (items awaiting review), not a generic dashboard.
- **Artwork identifies; evidence decides.** Covers lead in discovery and release views. In review and import, evidence, consequences and actions outrank artwork size.
- **Make decisions legible.** Review and import screens read in this order: release identity, matching evidence and confidence, file-operation consequences, next action. Show uncertainty. Keep the existing safety and undo paths.
- **Balance density.** Headings, alignment and spacing before decorative containers.
- **Reuse.** Extend existing Jinja macros and CSS. Make a shared component only for a pattern that repeats; keep one-off layouts on their page. No new UI stack.
- **Check on change.** When production templates change, check home, discovery, match review and import at phone and desktop widths in both modes.

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
- Sizes: `art-xs` 40px (table rows), `art-sm` 64px (review and plan headers), `art-md` 120px (release detail and follow on phones), `art-lg` 176px (discovery and release detail from 48rem).
- Missing cover: a `surface-sunken` square with a hairline border and the title's first letter in `ink-muted`, labelled "No cover art for …".

### State and motion
- Focus: a 2px solid `focus` outline with a 2px offset on every interactive element. Never remove it.
- Hover: darken to `accent-strong`, or strengthen a border to `ink`. Disabled: 50% opacity, with a reason in text nearby.
- Motion: colour and opacity only, 150 ms at most, all of it off under `prefers-reduced-motion: reduce`.

### Layout behaviour
- No page-level horizontal scroll at any width. Wide tables scroll inside `.table-wrap`.
- Review on narrow screens, in order: ActionBar (sticky), ScoreBreakdown, candidate (cover, tracklist), source files, AudioPreview, full evidence tables, other actions. Nothing is hidden behind a toggle.
- Nav, action bars and button rows wrap instead of collapsing into menus. Every page works without JS.

## Iconography

MixSync has no icon set. Status glyphs are Unicode geometric shapes drawn by CSS: ● confident/ok, ◐ needs review, ✕ rejected/error, ○ neutral/pending, ▲ warning, i info. Sort arrows are ↑ and ↓. Everything else is a word. Don't add an icon font or SVG sprite unless a pattern repeats and words fail. If you do, add it here first.

## Components

Classes are in `components/bundle.css` and `src/mixsync/web/static/app.css`. Macros: `macros/forms.html` (`csrf`, `plan_action`), `macros/ui.html` (`status`), `macros/media.html` (`artwork`). Each component has `components/<Name>/preview.html`. No component JavaScript.

Shared now: DataTable, StatusTag, Button, Field, Notice, ConsequenceSummary, Artwork. Local to one page: WorkQueue, ScoreBreakdown.

| Component | Where | Rules |
|---|---|---|
| AppNav | `base.html` | `<nav aria-label="Main">`, `aria-current="page"`. Groups: **Find** (Discover, Watchlist, Requests; Discover and Watchlist need `request`), **Resume** (Review, Unverified; Unverified needs `library.edit`), **Library** (Fragments, Migration; Migration needs `admin`). Brand links home; Account and Log out sit at the end (`.end`). A group hides when all its links are hidden. Review and Unverified carry a mono `.count` when above zero. Wraps, no menu. `.group-name` is a visual label only. |
| WorkQueue | home | `.queue`: count (`.n`), what it is, resume action. Urgency order: review, promotions, unverified, fragments, failed requests. First non-zero row's action is the page's `primary`. Zero rows stay, muted (`li.empty`), "Nothing to …". Not a dashboard: no charts. |
| ActionBar | review detail, plan | `.actionbar`, sticky, `role="region" aria-label="Decision"`, one CSRF `<form>` per action. Order: StatusTag, identity in `body-strong`, one line of top evidence in `ink-muted` (`.summary`), then `.actions`: one `primary`, alternatives, destructive last. If the primary action is unavailable, say why in the summary and omit the button; don't disable it. |
| ConsequenceSummary | plan, any file operation | `.consequence`: counts Move, Trash, Keep (`dd.trash` is `danger` above zero), one sentence (where files go, tags changed, originals to `.trash`, how to undo), then `forms.plan_action`. Above the data-safety threshold it asks for the typed change count. Required before any move, retag or trash. Never put a destructive confirm before it; keep "can be restored with Undo". |
| ScoreBreakdown | review evidence | `.score`: per `breakdown` entry a feature name (`.k`), a `.bar` of weighted penalty vs the reject threshold, the number (`.v`, mono) and the `reason` (`.why`). Bars over half the threshold get `.high`; number and reason carry the meaning. Top of Evidence, before vetoes. No inline styles (CSP `default-src 'self'`): use a width class or `<meter>`; the preview's inline width is illustrative. |
| DataTable | every record list | Wrap in `.table-wrap`. `th scope="col"` in `label` style on `surface-sunken`. Numbers: `class="n"` on `th` and `td`. IDs and timestamps: `.num`. Paths: `.path`. Sort links server-side, active header `aria-sort`. Selected row `aria-selected="true"`. Empty state is a sentence, not an empty table. No zebra, cell borders or card per row; don't hide columns on phones. |
| StatusTag | bands, statuses | `.tag` + `confident` ● / `review` ◐ / `reject` ✕ / `neutral` ○ (`.band-auto_accept`, `-review`, `-reject` for bands). Map raw values to words (`auto_accept` is "Confident"); `macros/ui.html` `status` does this. Not a button or link; a fifth state uses `neutral`. |
| Notice | results, warnings, errors | `.notice` with `ok`, `warn`, `error` (or `info` in the design bundle), or a bare `role="status"` / `role="alert"` element (app.css styles both). Bold headline states the fact, one sentence says what next. `status` for last-action results, `alert` for blockers. Under the title or above its form. No dismiss on errors, no more than two stacked. |
| Field | every form control | Visible `<label for>` (in tables, `aria-label`). `.input`; `.input.num` for short numbers. `.help` linked by `aria-describedby`. Invalid: `.field.invalid`, `aria-invalid="true"`, `.error` sentence (what is wrong, what is accepted), plus a `role="alert"` Notice above the form. Attributes match the route's validation. |
| Button | every action | Native `<button>`/`.btn`: secondary (default), `.primary` (exactly one per view, never two side by side), `.danger` (trash, discard, reject), `.danger.filled` (final confirm only, after a ConsequenceSummary), `.quiet` (reversible side actions). Label is verb + object. `<form method="post">` with `forms.csrf()`. Navigation is `<a>`, not a button. |
| Artwork | covers | `macros/media.html` `artwork(title, src, size)` emits `.art.art-<size>`; `.art.missing` is the fallback with `role="img" aria-label="No cover art for <title>"`. Only `/media/cover/<id>` sources. Never a stock image or emoji. |
| AudioPreview | review | Position, file name, length (mono, `ink-muted`), `<audio controls preload="none">` labelled with the file name, from `/media/preview/<id>/<n>`. Never autoplay. Beside the candidate tracklist from 48rem, after the evidence on phones. |
| Cover | design only | The brand banner in `components/Cover/preview.html`: an SVG tile grid built from `art-sm` cells and the wordmark. Not used by the app. |
| HomePage | design only | The home mockup in `components/HomePage/preview.html` (work queue, in flight, new music). Production home is `templates/pages/home.html`. |

## Why

- The positive status is blue-teal so it is not told from danger by hue (red–green).
- Nav wraps instead of collapsing: no JS, no hidden links.
- Review tables scroll in place and nothing sits behind a toggle, so evidence is never hidden.
- Artwork and AudioPreview are shared only once a second page uses them; WorkQueue and ScoreBreakdown stay local.
