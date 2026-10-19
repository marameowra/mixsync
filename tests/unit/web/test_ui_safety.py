"""Rendered safety information remains visible before any final file-operation action."""

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from mixsync.web.templating import templates


def _render(page: str, **context: Any) -> str:
    return templates.env.get_template(f"pages/{page}.html").render(
        request=Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 123),
            }
        ),
        user=None,
        csrf_token="test-token",
        typed_above=50,
        **context,
    )


def _decision(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 7,
        "band": "review",
        "status": "pending_review",
        "distance": 0.2,
        "scorer_version": 1,
        "release_mbid": "22222222-2222-2222-2222-222222222222",
        "breakdown": [{"key": "title", "penalty": 0.5, "weight": 2, "reason": "Different title"}],
        "vetoes": ["Missing track"],
        "evidence": {
            "source": {"files": [{"name": "matched.flac"}, {"name": "extra.flac"}]},
            "target": {"artist": "Artist", "title": "Album", "tracks": []},
            "assignment": [{"local_path": "/matched.flac", "track": 0}],
            "unassigned": ["extra.flac"],
            "runner_up": {"release_id": "r2", "title": "Other album", "gap": 0.012},
            "duration_deltas": [1.5],
            "acoustid": [{"score": 0.9, "recording_ids": ["r1"]}],
        },
    }
    return SimpleNamespace(**(values | changes))


def test_review_reading_order_keeps_all_matching_evidence_visible() -> None:
    html = _render("review_detail", d=_decision(), prefill={})
    markers = [
        'aria-label="Review actions"',
        "<h2>Score breakdown</h2>",
        "<h2>Candidate</h2>",
        "<h2>Source files</h2>",
        "<h2>Audio preview</h2>",
        "<h2>Full evidence</h2>",
        "<h2>Actions</h2>",
    ]
    assert [html.index(marker) for marker in markers] == sorted(
        html.index(marker) for marker in markers
    )
    assert "<strong>1 assigned file</strong>" in html
    assert "2 assigned files" not in html
    assert html.index('aria-label="Acceptance consequences"') < html.index(
        'action="/review/7/accept"'
    )
    for evidence in [
        "Different title",
        "Missing track",
        "Other album",
        "0.012",
        "AcoustID",
        "1.5 s",
    ]:
        assert evidence in html
    assert "Unassigned files are not imported by Accept" in html and "extra.flac" in html
    assert "read-only import tree stay untouched" in html
    assert "<details" not in html and "<audio controls" in html


@pytest.mark.parametrize("status", ["pending_review", "accepted"])
def test_acceptance_requires_a_release_and_pending_review(status: str) -> None:
    html = _render("review_detail", d=_decision(release_mbid=None, status=status), prefill={})
    assert 'action="/review/7/accept"' not in html
    if status == "pending_review":
        assert "disabled>Accept</button>" in html
        assert "No MusicBrainz release was found" in html
    else:
        assert 'action="/review/7/reject"' not in html
        assert 'action="/review/7/unverified"' not in html


@pytest.mark.parametrize("action", ["confirm", "resume", "undo"])
@pytest.mark.parametrize("changes", [50, 51])
def test_plan_consequences_precede_confirmation_and_count_only_changed_files(
    action: str, changes: int
) -> None:
    ops = [{"op": "trash", "old": {"path": f"a/{i}"}, "keeper": "keep"} for i in range(changes)]
    ops += [{"op": "keep", "old": {"path": "unchanged"}}]
    plan = SimpleNamespace(
        id=9,
        kind="merge",
        status="draft",
        error=None,
        ops=ops,
        release_group_mbid=None,
        canonical_release_mbid=None,
    )
    html = _render(
        "plan", plan=plan, counts={"move": 0, "trash": changes, "keep": 1}, allowed={action}
    )
    assert "0 to move and retag" in html and f"{changes} to trash" in html
    assert "1 to keep unchanged" in html and "AcoustID submissions" in html
    assert "go to .trash" in html and "restored with Undo" in html
    assert html.index('aria-label="Plan consequences"') < html.index(
        f'action="/library/plans/9/{action}"'
    )
    assert 'class="btn danger filled"' in html
    assert 'class="plan-op"' in html and 'class="plan-file"' in html
    assert 'name="csrf_token" value="test-token"' in html
    assert ('name="count"' in html) == (changes > 50)
    if changes > 50:
        assert "Type 51 to confirm" in html
