# `web/static`

Static assets served at `/static`. No build step.

## Contents
```
app.css                 the whole stylesheet, hand-written
vendor/htmx.min.js      pinned version, vendored (no CDN)
vendor/htmx-ext-sse.js  pinned version, vendored
vendor/VERSIONS.md      name, version, source URL, sha256 for each vendored file
icons/                  a few inline-able SVGs only if needed
```

## CSS rules (restrained by design)
- System font stack. One accent color plus neutrals. Light and dark through `prefers-color-scheme`.
- Layout uses CSS grid/flex only. Mobile-first; one breakpoint (about 48rem) unless more are proven necessary.
- Spacing and type scale defined once as custom properties in `:root`.
- Transitions ≤ 150 ms, only on color/opacity. Everything is disabled under `prefers-reduced-motion: reduce`.
- No CSS framework, no web fonts, no animation libraries.
- Tables scroll horizontally inside their container; the page never does.

## Vendoring rules
- Upgrade a vendored file deliberately: update the file and `VERSIONS.md` in the same commit.
- No runtime fetches from third-party origins (works offline on a LAN; CSP `'self'`).

## Design docs
[ADR 0003](../../../../docs/decisions/0003-htmx-server-rendered-ui.md)
