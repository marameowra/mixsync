"""Rendered safety information remains visible before any final file-operation action."""

from datetime import datetime
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
            "source": {
                "files": [
                    {"name": "matched.flac", "bitrate": 320, "duration": 61, "size": 1_000_000},
                    {"name": "extra.flac"},
                ]
            },
            "target": {
                "artist": "Artist",
                "title": "Album",
                "tracks": [{"position": 1, "title": "Song", "length": 61}],
            },
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
        'aria-label="Decision"',
        "<h2>Score breakdown</h2>",
        "<h2>Candidate</h2>",
        "<h2>Source files</h2>",
        "<h2>Audio preview</h2>",
        "<h2>Full evidence</h2>",
        "<h3>Raw score details</h3>",
        "<h2>Actions</h2>",
    ]
    assert [html.index(marker) for marker in markers] == sorted(
        html.index(marker) for marker in markers
    )
    assert "<strong>1 assigned file</strong>" in html
    assert "2 assigned files" not in html
    assert "contribution to distance" in html and "Different title" in html
    assert html.index('aria-label="Acceptance consequences"') < html.index(
        'action="/review/7/accept"'
    )
    assert html.index("<h2>Audio preview</h2>") < html.index("<h3>Raw score details</h3>")
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
    assert '<span class="path">matched.flac</span>' in html
    assert '<td class="n">320</td><td class="n">1:01</td><td class="n">1.0 MB</td>' in html
    assert '<td class="n">1</td><td>Song</td><td class="n">1:01</td>' in html
    assert "<details" not in html and "<audio controls" in html


def test_review_list_renders_distance_as_numeric_data() -> None:
    d = _decision(created_at=datetime(2026, 10, 20))
    html = _render("review_list", items=[d], sort="score", order="asc")
    assert '<td class="n">0.200</td>' in html


def test_score_meters_show_weighted_contributions_without_losing_raw_evidence() -> None:
    d = _decision(
        breakdown=[
            {"key": "title", "penalty": 0.5, "weight": 2, "reason": "Title mismatch"},
            {"key": "artist", "penalty": 0.3, "weight": 1, "reason": "Artist mismatch"},
        ]
    )
    html = _render("review_detail", d=d, prefill={})
    assert 'value="0.333" aria-label="title contribution to distance"' in html
    assert 'value="0.100" aria-label="artist contribution to distance"' in html
    assert '0.500</td><td class="n">2</td><td>Title mismatch' in html
    assert '0.300</td><td class="n">1</td><td>Artist mismatch' in html

    zero_weight = _decision(
        breakdown=[
            {"key": "title", "penalty": 0.5, "weight": 0, "reason": "Title mismatch"},
            {"key": "artist", "penalty": 0.3, "weight": 0, "reason": "Artist mismatch"},
        ]
    )
    zero_html = _render("review_detail", d=zero_weight, prefill={})
    assert zero_html.count('value="0.000" aria-label=') == 2
    assert "nan" not in zero_html.lower()


@pytest.mark.parametrize("status", ["pending_review", "accepted"])
def test_acceptance_requires_a_release_and_pending_review(status: str) -> None:
    html = _render("review_detail", d=_decision(release_mbid=None, status=status), prefill={})
    assert 'action="/review/7/accept"' not in html
    if status == "pending_review":
        assert "No MusicBrainz release found" in html
        assert ">Accept</button>" not in html
    else:
        assert 'action="/review/7/reject"' not in html
        assert 'action="/review/7/unverified"' not in html


@pytest.mark.parametrize("action", ["confirm", "resume", "undo"])
@pytest.mark.parametrize("changes", [50, 51])
def test_plan_consequences_precede_confirmation_and_count_only_changed_files(
    action: str, changes: int
) -> None:
    ops = [
        {"op": "trash", "done": False, "old": {"path": f"a/{i}"}, "keeper": "keep"}
        for i in range(changes)
    ]
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
    assert "<dt>Move</dt><dd>0</dd>" in html
    assert f'<dt>Trash</dt><dd class="trash">{changes}</dd>' in html
    assert "<dt>Keep</dt><dd>1</dd>" in html
    assert "0 files to move and retag" in html and "1 file to keep unchanged" in html
    assert "AcoustID submissions" in html
    assert ".trash" in html and "restored with Undo" in html
    assert html.index('aria-label="Plan consequences"') < html.index(
        f'action="/library/plans/9/{action}"'
    )
    assert 'id="plan-actions" class="actionbar" role="region" aria-label="Plan decision"' in html
    assert 'class="tag status neutral"' in html and "Merge" in html
    assert 'class="confirm"' in html
    if action == "confirm":
        assert 'class="btn danger filled"' in html
    elif action == "resume":
        assert 'class="btn danger"' in html
        assert "filled" not in html
    else:
        assert 'class="btn" type="submit">Undo merge</button>' in html
        assert "filled" not in html
    assert 'class="plan-op"' in html and 'class="plan-file"' in html
    assert 'name="csrf_token" value="test-token"' in html
    assert ('name="count"' in html) == (changes > 50)
    if changes > 50:
        assert "Type 51 to confirm" in html
        assert 'class="input num" name="count"' in html
        assert 'inputmode="numeric"' in html


def test_plan_confirm_without_trash_is_the_primary_action() -> None:
    plan = SimpleNamespace(
        id=9,
        kind="merge",
        status="draft",
        error=None,
        ops=[
            {
                "op": "move",
                "done": False,
                "old": {"path": "a/1", "status": "unverified", "release_mbid": "r1"},
                "new_path": "b/1",
                "tags": {
                    "status": "verified",
                    "release_id": "r2",
                    "recording_id": "r3",
                    "disc": 1,
                    "track": 1,
                    "title": "Song",
                    "album": "Album",
                },
                "track_id": 1,
            }
        ],
        release_group_mbid=None,
        canonical_release_mbid=None,
    )
    html = _render(
        "plan", plan=plan, counts={"move": 1, "trash": 0, "keep": 0}, allowed={"confirm"}
    )
    assert 'class="btn primary" type="submit">Confirm merge: move 1, trash 0</button>' in html
    assert 'class="btn danger filled"' not in html


def test_plan_undo_of_a_move_uses_outlined_danger() -> None:
    plan = SimpleNamespace(
        id=9,
        kind="merge",
        status="done",
        error=None,
        ops=[
            {
                "op": "move",
                "done": True,
                "old": {"path": "a/1", "status": "verified", "release_mbid": "r1"},
                "new_path": "b/1",
                "tags": {
                    "status": "verified",
                    "release_id": "r2",
                    "recording_id": "r3",
                    "disc": 1,
                    "track": 1,
                    "title": "Song",
                    "album": "Album",
                },
                "track_id": 1,
            }
        ],
        release_group_mbid=None,
        canonical_release_mbid=None,
    )
    html = _render("plan", plan=plan, counts={"move": 1, "trash": 0, "keep": 0}, allowed={"undo"})
    assert 'class="btn danger" type="submit">Undo merge</button>' in html
    assert 'class="btn danger filled"' not in html


def test_discard_plan_is_danger_outlined_and_has_fragment_target() -> None:
    plan = SimpleNamespace(
        id=9,
        kind="merge",
        status="draft",
        error=None,
        ops=[],
        release_group_mbid=None,
        canonical_release_mbid=None,
    )
    html = _render(
        "plan", plan=plan, counts={"move": 0, "trash": 0, "keep": 0}, allowed={"discard"}
    )
    assert 'id="plan-actions" class="actionbar"' in html
    assert 'action="/library/plans/9/discard"' in html
    assert 'class="btn danger" type="submit">Discard plan</button>' in html
    assert 'class="btn danger filled"' not in html


@pytest.mark.parametrize(("file_count", "expected"), [(1, "1 file"), (2, "2 files")])
def test_unverified_import_consequence_pluralizes_files(file_count: int, expected: str) -> None:
    d = _decision()
    d.evidence["source"]["files"] = [{"name": f"{i}.flac"} for i in range(file_count)]
    html = _render("review_detail", d=d, prefill={})
    assert (
        f"Imports <strong>{expected}</strong> with the tags below and no MusicBrainz ids." in html
    )
    assert "Copies are verified before downloaded sources are released" in html
    assert "read-only import tree stays untouched" in html
