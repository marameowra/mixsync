"""Tracks filed under an older path template (ADR 0019), and where the current one puts them."""

from pathlib import Path

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from mixsync.db.models.library import Track
from mixsync.library import paths, tags


def drifted(template: str) -> ColumnElement[bool]:
    """Rows whose path was rendered with another template. Null (not backfilled yet) is not."""
    return Track.path_template.is_not(None) & (Track.path_template != template)


def drift(s: Session, template: str) -> int:
    return s.scalar(select(func.count()).where(drifted(template))) or 0


def backfill(s: Session, template: str) -> None:
    """Rows from before ADR 0019 are taken to be filed under the configured template."""
    s.execute(update(Track).where(Track.path_template.is_(None)).values(path_template=template))


def _num(v: str | None) -> int:
    return int(v) if v and v.isascii() and v.isdigit() else 1


def render(template: str, file: Path, rel: str) -> str:
    """The path `template` gives the file from its own tags (which are not changed)."""
    f = tags.read_fields(file)
    year = tags.year_of(f.get("date", ""))
    return paths.render(
        template,
        albumartist=f.get("albumartist") or f.get("artist", ""),
        album=f.get("album", ""),
        year=int(year) if year else None,
        disc=_num(f.get("discnumber")),
        track=_num(f.get("tracknumber")),
        title=f.get("title") or Path(rel).stem,
        ext=Path(rel).suffix,
        genre=f.get("genre") or "Unknown Genre",
    )
