from dataclasses import replace

from mutagen.mp3 import MP3

from .conftest import Env
from .fakes import ALBUM, COVER
from .test_pipeline import FIRST, SECOND, request, status


def embedded(env: Env) -> list[bytes | None]:
    return [
        next((f.data for f in MP3(env.data_dir / "library" / rel).tags.getall("APIC")), None)  # pyright: ignore[reportOptionalMemberAccess]
        for rel in (FIRST, SECOND)
    ]


def with_embed(env: Env) -> None:
    env.pipeline = replace(env.pipeline, embed_cover=True)
    env.handlers = env.pipeline.handlers()


async def import_alpha(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "imported"


async def test_embed_cover_fetches_once_for_folder_and_files(env: Env) -> None:
    with_embed(env)
    await import_alpha(env)
    assert embedded(env) == [COVER, COVER]
    assert (env.data_dir / "library" / FIRST).with_name("cover.jpg").read_bytes() == COVER
    assert env.coverart.calls == [ALBUM.release_id]


async def test_cover_not_embedded_by_default(env: Env) -> None:
    await import_alpha(env)
    assert embedded(env) == [None, None]


async def test_oversized_cover_is_not_embedded(env: Env) -> None:
    with_embed(env)
    big = b"\xff\xd8" + b"x" * (1024 * 1024)

    async def front(release: str, group: str | None) -> bytes:
        return big

    env.coverart.front = front  # type: ignore[method-assign]
    await import_alpha(env)
    assert embedded(env) == [None, None]


async def test_cover_fetch_failure_does_not_fail_import(env: Env) -> None:
    with_embed(env)

    async def front(release: str, group: str | None) -> bytes:
        raise RuntimeError("caa down")

    env.coverart.front = front  # type: ignore[method-assign]
    await import_alpha(env)
    assert embedded(env) == [None, None]
