# Proposed answers to the open decisions

These answer the open decisions in `docs/design/ui-style.md`. They are proposals. Check each on home, discovery, match review and import, at phone and desktop widths, in both modes, before adopting it.

| Decision | Proposal |
|---|---|
| Neutral palette and accent | Warm-grey neutrals (`canvas`, `surface`, `surface-sunken`, `line`, `ink`, `ink-muted`) and one cobalt `accent`. All text pairs pass 4.5:1 and control borders pass 3:1, in both themes. |
| Type, spacing, grid | System stack; 8 styles from `display` 32/36 to `label` 12/16, plus mono `data` 13/18. 4px spacing base in 8 steps. 12 columns from one 48rem breakpoint, 72rem max. |
| Navigation groups | **Find**: Discover, Watchlist, Requests. **Resume**: Review, Unverified, each with a waiting count. **Library**: Fragments, Migration. Account and Log out sit at the end. Groups hide when the person's capabilities hide all their links. |
| Artwork size | `art-lg` 176px in discovery and release views, `art-md` 120px on phones, `art-sm` 64px in review and plan headers, `art-xs` 40px in table rows. A missing cover gets a lettered sunken tile. |
| Review on narrow screens | Order: ActionBar (sticky), ScoreBreakdown, candidate (cover, tracklist), source files, AudioPreview, then full evidence tables, then other actions. Nothing is hidden behind a toggle. Tables scroll in place. |
| Confidence, warning, error, destructive | StatusTag: ● Confident (`positive`), ◐ Needs review (`caution`), ✕ Rejected (`danger`), ○ neutral. Notices use the same glyphs, plus ▲ for warnings. Destructive actions are `danger` buttons, filled only for the final confirm, and always after a ConsequenceSummary. |
| Shared templates | Shared now: DataTable wrapper, StatusTag, Button, Field, Notice, ConsequenceSummary (extends `forms.plan_action`). Shared once a second page uses it: Artwork, AudioPreview (`macros/media.html`). Kept local: WorkQueue (home), ScoreBreakdown (review). |
