"""Canned MusicBrainz and cover art answers so preview pages work offline."""

import base64

from mixsync.core.types import EntityRef, ReleaseGroupHit, ReleaseRef

LONG_NAME = "The Extraordinarily Long-Named Orchestra of " + "Nowhere in Particular " * 8
_A = "aaaaaaaa-0000-0000-0000-00000000000{}"
COVER_RELEASE = "11111111-1111-1111-1111-111111111114"
COVER_GROUP = "11111111-1111-1111-1111-111111111115"
GROUP_FALLBACK_RELEASE = "11111111-1111-1111-1111-111111111116"
NO_COVER_RELEASE = "11111111-1111-1111-1111-111111111117"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l5QAAAAASUVORK5CYII="
)
_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AT//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AT//2Q=="
)


class FakeCoverArt:
    async def front(self, release: str | None, group: str | None) -> bytes | None:
        if release == COVER_RELEASE:
            return _JPEG
        if release == NO_COVER_RELEASE:
            return None
        return _PNG if group == COVER_GROUP else None


class FakeWatch:
    async def search_entities(self, kind: str, name: str) -> list[EntityRef]:
        if name == "none":  # the empty-result page
            return []
        return [
            EntityRef(_A.format(1), f"{name} Quartet", "UK jazz band"),
            EntityRef(_A.format(2), LONG_NAME[:200], "very long name"),
            EntityRef(_A.format(3), "坂本龍一 (Ryuichi Sakamoto)"),
        ]

    async def entity_name(self, kind: str, mbid: str) -> str:
        return "Preview Artist"

    async def artist_discography(self, mbid: str) -> tuple[list[ReleaseGroupHit], bool]:
        hits = [
            ReleaseGroupHit(
                COVER_GROUP if i == 1 else f"bbbbbbbb-0000-0000-0000-{i:012}",
                title,
                "Preview Artist",
                kind,
                date,
                (f"cccccccc-0000-0000-0000-{i:012}",),
            )
            for i, (title, kind, date) in enumerate(
                [
                    ("First Album", "album", "2001-05-01"),
                    ("Second Album", "album", "2004"),
                    ("A Small EP", "ep", "2006-03"),
                    ("Hit Single", "single", "2007-01-01"),
                    ("Live in 東京 " + "x" * 150, "live", "2010-10-10"),
                    ("Undated Oddity", "other", ""),
                ],
                1,
            )
        ]
        return hits, True  # truncated: shows the notice

    async def new_release_groups(
        self, kind: str, mbid: str, since: str | None, *, fresh: bool = False
    ) -> list[ReleaseGroupHit]:
        return []

    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]:
        return []

    async def label(self, kind: str, mbid: str, *, cached_only: bool = False) -> str | None:
        return None if cached_only else f"Preview {kind} \u2014 Preview Artist"
