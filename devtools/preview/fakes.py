"""Canned MusicBrainz answers so the watchlist pages work offline."""

from mixsync.core.types import EntityRef, ReleaseGroupHit, ReleaseRef

LONG_NAME = "The Extraordinarily Long-Named Orchestra of " + "Nowhere in Particular " * 8
_A = "aaaaaaaa-0000-0000-0000-00000000000{}"


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
                f"bbbbbbbb-0000-0000-0000-{i:012}",
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
