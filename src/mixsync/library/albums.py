"""Albums as the library shows them, from the `tracks` name columns (ADR 0018); no file is read.

The one grouping rule for the browse page and the unverified list: verified tracks form an album
by release group; every other track by (album artist, album, year), a year-less group joining
the one dated group with the same artist and album. Tracks with empty album artist and album
are "untagged"; tracks whose names are not filled in yet are "pending"."""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from mixsync.db.models.library import Track

Key = tuple[str, str, str]  # album artist, album, year
PAGE = 50


def fold(keys: Iterable[Key]) -> dict[Key, Key]:
    """Each key's album: a year-less key joins the one dated key with the same artist and album."""
    ks = set(keys)
    dated: dict[tuple[str, str], list[Key]] = {}
    for k in ks:
        if k[2]:
            dated.setdefault(k[:2], []).append(k)
    return {k: d[0] if not k[2] and len(d := dated.get(k[:2], [])) == 1 else k for k in ks}


@dataclass(frozen=True)
class Album:
    kind: str  # verified | unverified | untagged | pending
    artist: str
    album: str
    year: str
    tracks: int
    group: str = ""  # a verified album's release group


def _untagged() -> ColumnElement[bool]:
    return and_(Track.album_artist == "", Track.album == "")


def _by_group() -> ColumnElement[bool]:
    return and_(Track.status == "verified", Track.release_group_mbid.is_not(None))


def _named() -> ColumnElement[bool]:
    return and_(Track.album.is_not(None), ~_untagged())


def _matching(text: str) -> list[ColumnElement[bool]]:
    like = text.lower()
    return [
        or_(
            func.lower(Track.album_artist).contains(like, autoescape=True),
            func.lower(Track.album).contains(like, autoescape=True),
        )
    ] * bool(like)


def search(db: Session, text: str = "") -> list[Album]:
    """Every album matching `text` (case-insensitive, in album artist or album), sorted by album
    artist then album; untagged and pending last. Matching is in SQL; the year-less merge and
    the sort run over one row per album."""
    # shortcut: sorts and pages one row per album in Python; move to SQL if albums reach ~100k.
    text = text.strip()
    verified = select(
        Track.release_group_mbid,
        func.max(Track.album_artist),
        func.max(Track.album),
        func.max(Track.year),
        func.count(),
    ).where(_by_group(), _named(), *_matching(text))
    named = select(Track.status, Track.album_artist, Track.album, Track.year, func.count()).where(
        ~_by_group(), _named(), *_matching(text)
    )
    rows = [
        Album("verified", aa or "", al or "", y or "", n, g or "")
        for g, aa, al, y, n in db.execute(verified.group_by(Track.release_group_mbid))
    ]
    raw = {
        (st, aa or "", al or "", y or ""): n
        for st, aa, al, y, n in db.execute(
            named.group_by(Track.status, Track.album_artist, Track.album, Track.year)
        )
    }
    into = fold((aa, al, y) for _, aa, al, y in raw)
    merged: dict[tuple[str, Key], int] = {}
    for (st, aa, al, y), n in raw.items():
        k = (st, into[(aa, al, y)])
        merged[k] = merged.get(k, 0) + n
    rows += [Album(st, *k, n) for (st, k), n in merged.items()]
    rows.sort(key=lambda a: (a.artist.lower(), a.album.lower(), a.year, a.kind))
    if not text:
        for kind, cond in (("untagged", _untagged()), ("pending", Track.album.is_(None))):
            if n := db.scalar(select(func.count()).where(cond)):
                rows.append(Album(kind, "", "", "", n))
    return rows


def tracks_of(db: Session, kind: str, group: str, key: Key) -> list[Track]:
    """The tracks of one album as `search` lists it."""
    where: list[ColumnElement[bool]]
    if kind == "untagged":
        where = [_untagged()]
    elif kind == "pending":
        where = [Track.album.is_(None)]
    elif kind == "verified":
        where = [_by_group(), _named(), Track.release_group_mbid == group]
    else:
        aa, al, _ = key
        where = [~_by_group(), _named(), Track.status == kind]
        where += [Track.album_artist == aa, Track.album == al]
        years = db.scalars(select(Track.year).where(*where).distinct()).all()
        into = fold((aa, al, y or "") for y in years)
        where.append(Track.year.in_([y for (_, _, y), k in into.items() if k == key]))
    return list(db.scalars(select(Track).where(*where).order_by(Track.path)))
