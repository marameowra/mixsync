import re

# "feat." and friends run to the closing bracket or the end; Soulseek treats "-" as exclusion.
_FEAT = re.compile(r"[(\[]?\b(?:feat|ft|featuring)\b\.?[^)\]]*[)\]]?", re.IGNORECASE)
_PUNCT = re.compile(r"[\W_]+")


def _words(s: str) -> str:
    return " ".join(s.split())


def queries(artist: str, album: str) -> list[str]:
    """Search strings to try in order: the plain one first, then looser fallbacks."""
    plain = _words(f"{artist} {album}")
    album_nf = _FEAT.sub(" ", album)
    no_feat = _words(f"{_FEAT.sub(' ', artist)} {album_nf}")
    out = [
        plain,
        no_feat,
        _words(_PUNCT.sub(" ", no_feat)),
        _words(_PUNCT.sub(" ", album_nf)),
    ]
    return list(dict.fromkeys(q for q in out if q))
