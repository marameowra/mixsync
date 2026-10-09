# `web/templates`

Jinja2 templates. Server-rendered HTML that reflows on any screen.

## Layout
```
base.html            <html>, <head>, nav, flash area, <main>; loads app.css + htmx
pages/<name>.html    one per route page; extends base.html
partials/_<name>.html  HTMX-swappable fragments (leading underscore)
macros/forms.html    inputs, buttons, confirm-with-count, csrf field
macros/media.html    track row, cover image, <audio> preview, score breakdown table
```

## Rules
- Autoescape is on everywhere. Never `|safe` on user- or API-supplied data.
- Semantic HTML first (`<nav>`, `<main>`, `<table>` for tabular data, `<form>` for every action).
- Each page renders fully without JS. HTMX attributes enhance it.
- Every form includes `{{ forms.csrf() }}`.
- No inline styles and no inline scripts (keeps a strict CSP possible: `default-src 'self'`).
- Accessibility: labels on all inputs, visible focus, buttons are `<button>`, color is never the only signal (bands show text plus color).

## Design docs
[ADR 0003](../../../../docs/decisions/0003-htmx-server-rendered-ui.md)
