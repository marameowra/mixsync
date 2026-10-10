from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Pipeline
from mixsync.core.clock import SystemClock
from mixsync.core.matching import AlbumInfo
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer, ImportResult

ALBUM = AlbumInfo("A", "B", (), release_id="R", release_group_id="G")
REL = "A/B (2000)/01-01 T.flac"
COVER = "A/B (2000)/cover.jpg"
IMPORTED = [ImportResult(Path("x"), REL)]


class FakeArt:
    def __init__(self, data: bytes | None = b"img", error: Exception | None = None) -> None:
        self.data, self.error, self.calls = data, error, 0

    async def front(self, release_mbid: str, release_group_mbid: str | None) -> bytes | None:
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


async def run_cover(
    ops: FileOps, sessions: sessionmaker[Session], art: FakeArt, results: list[ImportResult]
) -> None:
    importer = Importer(ops, sessions, SystemClock(), "")
    unused = {f: cast(Any, None) for f in Pipeline.__dataclass_fields__}
    p = Pipeline(**unused | {"importer": importer, "coverart": art})
    await p._cover(ALBUM, results, "b")  # pyright: ignore[reportPrivateUsage]


async def test_cover_written_once(ops: FileOps, sessions: sessionmaker[Session]) -> None:
    art = FakeArt()
    await run_cover(ops, sessions, art, IMPORTED)
    await run_cover(ops, sessions, art, IMPORTED)  # a retried import
    assert (ops.library / COVER).read_bytes() == b"img" and art.calls == 1


async def test_existing_cover_is_kept(ops: FileOps, sessions: sessionmaker[Session]) -> None:
    (ops.library / "A/B (2000)").mkdir(parents=True)
    (ops.library / COVER).write_bytes(b"mine")
    art = FakeArt()
    await run_cover(ops, sessions, art, IMPORTED)
    assert (ops.library / COVER).read_bytes() == b"mine" and art.calls == 0


@pytest.mark.parametrize("art", [FakeArt(None), FakeArt(error=RuntimeError("caa down"))])
async def test_missing_art_or_error_does_not_fail(
    ops: FileOps, sessions: sessionmaker[Session], art: FakeArt
) -> None:
    await run_cover(ops, sessions, art, IMPORTED)
    assert not (ops.library / COVER).exists()


async def test_nothing_imported_no_lookup(ops: FileOps, sessions: sessionmaker[Session]) -> None:
    art = FakeArt()
    await run_cover(ops, sessions, art, [ImportResult(Path("x"), None, "e")])
    assert art.calls == 0
