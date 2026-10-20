# swiss

Reference prototype of the [design system](../../system/README.md) as a preview theme. Production now implements the system in `src/mixsync/web/static/app.css` and the real templates, so this theme is not kept in step with them.

View: `uv run python -m devtools.preview`, then `/__preview?theme=swiss`.

- `static/theme.css` replaces `app.css`: tokens, component CSS, and fallback styling for bare elements.
- `templates/base.html`: the grouped nav (Find, Resume, Library).
- `templates/pages/home.html`: the "Waiting on you" work queue.
