"""The library browse page and the name columns behind it (ADR 0018): grouping, search, paging,
the backfill job and the album delete, against the `world` fixture."""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire import recheck
from mixsync.core.jobs import JobKind
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.library import albums
from mixsync.library.name_backfill import NameBackfill

from .test_delete import _login, _paths, _post, _roundtrip, _setup
from .test_merge import GROUP, OLD, World, world  # noqa: F401  # world is a fixture


def _add(
    sessions: sessionmaker[Session],
    rows: list[tuple[str, str | None, str | None, str | None]],  # status, artist, album, year
    prefix: str = "t",
) -> None:
    with sessions.begin() as s:
        for i, (status, aa, al, year) in enumerate(rows):
            s.add(
                Track(
                    path=f"{prefix}/{i}.flac",
                    status=status,
                    batch_id="b",
                    imported_at=datetime.now(UTC),
                    album_artist=aa,
                    album=al,
                    year=year,
                    release_group_mbid=f"rg-{prefix}-{i}" if status == "verified" else None,
                )
            )


def _clear(w: World) -> None:
    with w.sessions.begin() as s:
        for t in s.scalars(select(Track)):
            s.delete(t)


def test_verified_album_from_migration_deletes_as_one_undoable_plan(world: World) -> None:  # noqa: F811
    c = _setup(world)
    every = _paths(world)
    assert len(every) == 5
    with c:
        tok = _login(c, "root")
        page = c.get("/library/albums").text
        assert "Band" in page and "Verified" in page and "Names pending" not in page
        album = c.get("/library/album", params={"kind": "verified", "group": GROUP}).text
        assert all(p in album for p in every) and "Delete album" in album
        _roundtrip(world, c, tok, "/library/album/delete", every, kind="verified", group=GROUP)
        assert world.names_match_files()  # the undo put every row back with its names


def test_same_record_verified_and_unverified_is_two_rows(world: World) -> None:  # noqa: F811
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.release_mbid != OLD).values(status="unverified"))
    with world.sessions() as s:
        rows = albums.search(s)
    assert sorted((a.kind, a.tracks) for a in rows) == [("unverified", 1), ("verified", 4)]
    with _setup(world) as c:
        _login(c, "root")
        page = c.get("/library/albums").text
    assert page.count("Verified</span>") == 1 and page.count("Unverified</span>") == 1


def test_unverified_list_and_browse_group_the_same_way(world: World) -> None:  # noqa: F811
    _clear(world)
    _add(
        world.sessions,
        [
            ("unverified", "A", "X", "2001"),
            ("unverified", "A", "X", "2001"),
            ("unverified", "A", "X", ""),  # joins its single dated twin
            ("unverified", "B", "Y", "2000"),
            ("unverified", "B", "Y", "2001"),
            ("unverified", "B", "Y", ""),  # two dated twins: stays alone
            ("unverified", "C", "Z", ""),
        ],
    )
    with world.sessions() as s:
        listed = {k: len(v) for k, v in recheck.group(recheck.read_unverified(s)).items()}
        browsed = {(a.artist, a.album, a.year): a.tracks for a in albums.search(s)}
        assert albums.tracks_of(s, "unverified", "", ("A", "X", "2001")).__len__() == 3
    assert (
        listed
        == browsed
        == {
            ("A", "X", "2001"): 3,
            ("B", "Y", "2000"): 1,
            ("B", "Y", "2001"): 1,
            ("B", "Y", ""): 1,
            ("C", "Z", ""): 1,
        }
    )


def test_untagged_last_and_pending_apart(world: World) -> None:  # noqa: F811
    _clear(world)
    _add(
        world.sessions,
        [
            ("unverified", "", "", ""),
            ("verified", "", "", ""),  # unreadable verified file: also untagged
            ("unverified", None, None, None),  # not filled yet
            ("unverified", "zed", "Last", "2000"),
            ("unverified", "Alpha", "First", "1999"),
        ],
    )
    with world.sessions() as s:
        rows = albums.search(s)
        assert [(a.kind, a.artist, a.tracks) for a in rows] == [
            ("unverified", "Alpha", 1),
            ("unverified", "zed", 1),
            ("untagged", "", 2),
            ("pending", "", 1),
        ]
        assert albums.search(s, "alpha")[0].artist == "Alpha"
        assert [a.kind for a in albums.search(s, "a")] == ["unverified", "unverified"]
        # pending tracks stay out of the unverified list until the job names them
        assert sum(len(v) for v in recheck.group(recheck.read_unverified(s)).values()) == 3
    with _setup(world) as c:
        tok = _login(c, "root")
        page = c.get("/library/albums").text
        assert "Untagged" in page and "Names pending" in page and "Names pending for 1" in page
        for kind in ("untagged", "pending"):
            html = c.get("/library/album", params={"kind": kind}).text
            assert "Delete album" not in html and "Delete track" in html
            assert _post(c, tok, "/library/album/delete", kind=kind) == 404
        # Untagged offers the unverified list's empty-album group delete (#270); pending does not
        untagged = c.get("/library/album", params={"kind": "untagged"}).text
        assert untagged.count("Delete this album from the library") == 1
        assert 'action="/library/unverified/delete"' in untagged
        pending = c.get("/library/album", params={"kind": "pending"}).text
        assert "Delete this album from the library" not in pending
        _login(c, "alice")  # no library.edit: no action at all
        plain = c.get("/library/album", params={"kind": "untagged"}).text
        assert "Delete this album" not in plain and "Delete track" not in plain


def test_backfill_fills_from_tags_without_writing_files(world: World) -> None:  # noqa: F811
    lib0 = world.lib()
    with world.sessions.begin() as s:
        s.execute(update(Track).values(album_artist=None, album=None, year=None))
        gone = s.scalars(select(Track).order_by(Track.id)).first()
        assert gone is not None
        gone_id = gone.id
    (world.ops.library / _paths(world, Track.id == gone_id)[0]).unlink()
    lib0.pop(_paths(world, Track.id == gone_id)[0])
    backfill = NameBackfill(world.sessions, world.merger.clock, world.ops)
    assert JobKind.NAME_BACKFILL in backfill.handlers()
    backfill.start_chains()
    backfill.start_chains()  # a live job is not doubled
    with world.sessions() as s:
        assert len(list(s.scalars(select(Job).where(Job.kind == JobKind.NAME_BACKFILL)))) == 1
        assert albums.search(s)[-1].kind == "pending"
    backfill.fill()
    assert world.lib() == lib0  # read only
    with world.sessions() as s:
        rows = s.scalars(select(Track).order_by(Track.id)).all()
        assert [(t.album_artist, t.album, t.year) for t in rows][0] == ("", "", "")
        assert {(t.album_artist, t.album) for t in rows[1:]} == {("Band", "Album")}
        assert {t.year for t in rows[1:]} == {"2000"}
        assert [a.kind for a in albums.search(s)][-1] == "untagged"  # the unreadable one
    backfill.start_chains()  # nothing left to fill: no new job
    with world.sessions() as s:
        assert len(list(s.scalars(select(Job).where(Job.kind == JobKind.NAME_BACKFILL)))) == 1


def _hrefs(html: str) -> list[str]:
    found = re.findall(r"kind=\w+&amp;group=[^&]*&amp;albumartist=([^&]*)&amp;album=([^&]*)", html)
    return [(unquote_plus(a), unquote_plus(b)) for a, b in found]


def test_search_and_paging_order(world: World) -> None:  # noqa: F811
    _clear(world)
    rows: list[Any] = [
        ("unverified", f"{'bravo' if i % 2 else 'Alpha'} {i:03d}", "Rec_%", "2000")
        for i in range(120)
    ]
    _add(world.sessions, rows)
    with _setup(world) as c:
        _login(c, "alice")  # no library.edit: browsing is for every user
        pages = [c.get("/library/albums", params={"page": n}).text for n in (1, 2, 3, 4)]
        artists = [a for p in pages for a, _ in _hrefs(p)]
        counts = [len(_hrefs(p)) for p in pages]
        assert counts == [50, 50, 20, 20]  # a page past the end is the last
        assert artists[:120] == sorted((r[1] for r in rows), key=str.lower)
        assert "Page 2 of 3" in pages[1] and "page=3" in pages[1] and "page=1" in pages[1]
        found = c.get("/library/albums", params={"q": "ALPHA 01"}).text
        assert [a for a, _ in _hrefs(found)] == [f"Alpha {n:03d}" for n in (10, 12, 14, 16, 18)]
        assert len(_hrefs(c.get("/library/albums", params={"q": "rec_%"}).text)) == 50  # literal
        assert len(_hrefs(c.get("/library/albums", params={"q": "recx"}).text)) == 0
        assert len(_hrefs(c.get("/library/albums", params={"q": "nope"}).text)) == 0
        assert "Alpha 010" in c.get("/library/albums", params={"q": "alpha 010"}).text


def test_permissions(world: World) -> None:  # noqa: F811
    c = _setup(world)
    group = {"kind": "verified", "group": GROUP}
    with c:
        alice = _login(c, "alice")  # a plain user
        for url, params in (("/library/albums", {}), ("/library/album", group)):
            html = c.get(url, params=params).text
            assert c.get(url, params=params).status_code == 200
            assert "Delete album" not in html and "/delete" not in html and "/tags" not in html
        assert _post(c, alice, "/library/album/delete", **group) == 403
        assert _post(c, alice, "/library/album/delete", kind="unverified") == 403
        root = _login(c, "root")
        html = c.get("/library/album", params=group).text
        assert "Delete album" in html and "/tags" in html and "/delete" in html
        assert _post(c, root, "/library/album/delete", kind="verified", group="nope") == 404
        c.cookies.clear()
        assert c.get("/library/albums").status_code in (303, 401)
