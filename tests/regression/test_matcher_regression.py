"""The matcher regression gate (ADRs 0005, 0006): every corpus case must keep its baselined band
and release. To accept a deliberate change: `uv run python -m devtools.regression.replay --update`
and commit the baseline diff. See docs/testing.md."""

import json

from devtools.regression.replay import BASELINE, diff, load, run


def test_corpus_is_seeded() -> None:
    assert len(load()) >= 20


def test_no_case_changed_band_or_release() -> None:
    changes = diff(json.loads(BASELINE.read_text()), run())
    assert not changes, "matcher results moved from the baseline:\n" + "\n".join(changes)
