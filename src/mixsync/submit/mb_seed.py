"""Field list for seeding the MusicBrainz release editor (POST to `SEED_URL`).

Field names checked 2026-10-10 against
https://musicbrainz.org/doc/Development/Release_Editor_Seeding (indexes are 0-based;
`length` is "MM:SS" or integer milliseconds, we send milliseconds).
"""

from collections.abc import Sequence
from dataclasses import dataclass

SEED_URL = "https://musicbrainz.org/release/add"
EDIT_NOTE = "Seeded by MixSync from local files."


@dataclass(frozen=True)
class Credit:
    name: str
    join_phrase: str = ""


@dataclass(frozen=True)
class SeedTrack:
    number: str
    name: str
    length_ms: int | None = None
    artists: Sequence[Credit] = ()  # only when it differs from the release artist


@dataclass(frozen=True)
class SeedMedium:
    tracks: Sequence[SeedTrack]
    format: str | None = None


@dataclass(frozen=True)
class SeedRelease:
    name: str
    artists: Sequence[Credit]
    mediums: Sequence[SeedMedium]
    types: Sequence[str] = ()  # release group types, e.g. "Album", "Compilation"
    status: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None


def _credits(prefix: str, credits: Sequence[Credit]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for i, c in enumerate(credits):
        p = f"{prefix}.names.{i}"
        out.append((f"{p}.name", c.name))
        if c.join_phrase:
            out.append((f"{p}.join_phrase", c.join_phrase))
    return out


def build_seed(r: SeedRelease) -> list[tuple[str, str]]:
    """Ordered (field, value) pairs; unknown fields are omitted."""
    f: list[tuple[str, str]] = [("name", r.name)]
    f += _credits("artist_credit", r.artists)
    f += [("type", t) for t in r.types]
    if r.status:
        f.append(("status", r.status))
    for part, v in (("year", r.year), ("month", r.month), ("day", r.day)):
        if v is not None:
            f.append((f"events.0.date.{part}", str(v)))
    if r.label:
        f.append(("labels.0.name", r.label))
    if r.catalog_number:
        f.append(("labels.0.catalog_number", r.catalog_number))
    if r.barcode:
        f.append(("barcode", r.barcode))
    for m, medium in enumerate(r.mediums):
        if medium.format:
            f.append((f"mediums.{m}.format", medium.format))
        for t, tr in enumerate(medium.tracks):
            p = f"mediums.{m}.track.{t}"
            f += [(f"{p}.number", tr.number), (f"{p}.name", tr.name)]
            if tr.length_ms is not None:
                f.append((f"{p}.length", str(tr.length_ms)))
            f += _credits(f"{p}.artist_credit", tr.artists)
    f.append(("edit_note", EDIT_NOTE))
    return f
