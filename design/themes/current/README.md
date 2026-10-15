# current

Empty on purpose: a theme with no `templates/` or `static/` shows today's UI.

## Make a theme
1. `mkdir -p design/themes/<name>/templates design/themes/<name>/static`
2. Copy any template from `src/mixsync/web/templates/` into `design/themes/<name>/templates/`
   (same relative path, e.g. `base.html` or `pages/home.html`) and edit it. Templates you do not
   copy fall through to the real ones.
3. Put CSS/JS in `static/`; it is served at `/themes/<name>/...`. Load it from your `base.html`.
4. Start `uv run python -m devtools.preview` and open `/__preview?theme=<name>`.

See `design/themes/demo` for a working example.
