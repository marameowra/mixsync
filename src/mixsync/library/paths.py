import re
import unicodedata
from collections.abc import Callable

SEGMENT_MAX_BYTES = 180  # leaves room for a collision suffix under the common 255-byte limit
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])$")
_EXT = ".{ext}"


def sanitize(s: str, max_bytes: int = SEGMENT_MAX_BYTES) -> str:
    """One path segment or field value: safe on Windows, macOS, Linux and SMB, never empty."""
    s = unicodedata.normalize("NFC", _ILLEGAL.sub("", s)).strip(" .")
    s = s.encode()[:max_bytes].decode(errors="ignore").strip(" .")  # cut on a character boundary
    if _RESERVED.match(s.split(".")[0].rstrip()):  # Windows also rejects "NUL.txt"
        s = "_" + s
    return s or "_"


def render(
    template: str,
    *,
    albumartist: str,
    album: str,
    year: int | None,
    disc: int,
    track: int,
    title: str,
    ext: str,
    genre: str = "Unknown Genre",
) -> str:
    """Relative "/" separated path. The template must end in ".{ext}". Field values are
    sanitized before substitution so they cannot add directories, and the extension survives
    the length cap."""
    if not template.endswith(_EXT):
        raise ValueError(f"path template must end in {_EXT}")
    if year is None:
        template = template.replace(" ({year})", "").replace("({year})", "")
    suffix = "." + sanitize(ext.lstrip("."), 10)
    parts = template.removesuffix(_EXT).split("/")
    out: list[str] = []
    for i, part in enumerate(parts):
        end = suffix if i == len(parts) - 1 else ""
        text = part.format(
            albumartist=sanitize(albumartist),
            album=sanitize(album),
            year=year,
            disc=disc,
            track=track,
            title=sanitize(title),
            genre=sanitize(genre),
        )
        out.append(sanitize(text, SEGMENT_MAX_BYTES - len(end)) + end)
    return "/".join(out)


def unique(rel: str, exists: Callable[[str], bool]) -> str:
    """First of rel, "name (2).ext", "name (3).ext"... that `exists` rejects. Never overwrites."""
    if not exists(rel):
        return rel
    head, slash, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem = name
    n = 2
    while exists(cand := f"{head}{slash}{stem} ({n}){dot}{ext}"):
        n += 1
    return cand
