"""ADR 0020: htmx is the only script. No inline <script>, on*= handlers or hx-on in a template."""

import json
import re
from pathlib import Path

import mixsync.web

TEMPLATES = Path(mixsync.web.__file__).parent / "templates"
HTMX_TAG = '<script src="/static/htmx-2.0.4.min.js" defer></script>'
# an on<event>= attribute: preceded by whitespace or a quote, as in ` onclick=` (not `data-on=`)
INLINE_HANDLER = re.compile(r"""[\s"'/]on[a-z]+\s*=""", re.IGNORECASE)


def test_no_template_has_inline_js() -> None:
    files = list(TEMPLATES.rglob("*.html"))
    assert files
    bad: list[str] = []
    for f in files:
        text = f.read_text().replace(HTMX_TAG, "")
        for needle in ("<script", "hx-on", "hx-vars", "javascript:"):
            if needle in text.lower():
                bad.append(f"{f.relative_to(TEMPLATES)}: {needle}")
        if INLINE_HANDLER.search(text):
            bad.append(f"{f.relative_to(TEMPLATES)}: on<event>= attribute")
    assert not bad


def test_base_loads_htmx_once_and_swaps_error_responses() -> None:
    base = (TEMPLATES / "base.html").read_text()
    assert base.count(HTMX_TAG) == 1
    # a rejected boosted form (4xx) must swap its error page in instead of failing silently
    m = re.search(r"""name="htmx-config" content='([^']*)'""", base)
    assert m
    rules = json.loads(m.group(1))["responseHandling"]
    assert {"code": "4..", "swap": True, "error": True} in rules
    assert not any(r["swap"] and r["code"].startswith("5") for r in rules)  # 5xx keeps polling
    assert json.loads(m.group(1))["allowEval"] is False  # CSP has no 'unsafe-eval' (#330)
    assert json.loads(m.group(1))["includeIndicatorStyles"] is False  # no injected <style>


def test_no_template_has_inline_style_or_trigger_filter() -> None:
    bad: list[str] = []
    for f in TEMPLATES.rglob("*.html"):
        text = f.read_text()
        name = str(f.relative_to(TEMPLATES))
        if re.search(r"<style|\sstyle\s*=", text, re.IGNORECASE):  # CSP has no 'unsafe-inline'
            bad.append(f"{name}: inline style")
        for m in re.finditer(r"""hx-trigger\s*=\s*("[^"]*"|'[^']*'|\S*)""", text, re.IGNORECASE):
            # `from:<selector>` may hold `[...]` (an attribute selector); any other `[` is a filter
            if "[" in re.sub(r"from:\S+", "", m.group(1)):  # filters need eval
                bad.append(f"{name}: hx-trigger filter")
        if re.search(r"""hx-vals\s*=\s*["']\s*js:""", text, re.IGNORECASE):  # eval
            bad.append(f"{name}: hx-vals js:")
    assert not bad


def test_only_the_seed_form_posts_off_origin() -> None:
    bad: list[str] = []
    for f in TEMPLATES.rglob("*.html"):
        for action in re.findall(r"""<form[^>]*\saction\s*=\s*"([^"]*)""", f.read_text()):
            # relative paths, route-built template expressions (`{{ '/x' ~ id }}`), the seed URL
            if not action.startswith(("/", "{{ '/")) and action != "{{ seed_url }}":
                bad.append(f"{f.relative_to(TEMPLATES)}: {action}")
    assert not bad  # form-action 'self' blocks them; the seed form's route relaxes it (ADR 0020)
