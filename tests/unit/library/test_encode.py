import shutil
import subprocess
from pathlib import Path

import mutagen
import pytest
from mutagen.flac import FLAC

from mixsync.core.errors import PermanentError
from mixsync.library.encode import _pcm_md5, to_flac

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

SINE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100"]
COVER = ["-f", "lavfi", "-i", "color=c=red:s=64x64:d=1"]


def ff(out: Path, *args: str, cover: bool = False) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", *SINE, *(COVER if cover else []), *args, str(out)]
    subprocess.run(cmd, check=True)
    return out


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    (tmp_path / "downloads").mkdir()
    return tmp_path / "downloads"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path / "work"


async def test_flac_level_0_shrinks_and_decodes_identically(downloads: Path, work: Path) -> None:
    src = ff(downloads / "a.flac", "-compression_level", "0")
    out = await to_flac(src, work, 8)
    assert out != src and out.stat().st_size < src.stat().st_size
    assert await _pcm_md5(out) == await _pcm_md5(src)


async def test_flac_that_does_not_shrink_is_kept(downloads: Path, work: Path) -> None:
    src = ff(downloads / "a.flac", "-compression_level", "8")
    assert await to_flac(src, work, 8) == src


async def test_wav_and_aiff_become_flac(downloads: Path, work: Path) -> None:
    for ext in ("wav", "aiff"):
        src = ff(downloads / f"a.{ext}")
        out = await to_flac(src, work / ext)
        assert out.suffix == ".flac" and isinstance(mutagen.File(out), FLAC)
        assert await _pcm_md5(out) == await _pcm_md5(src)


async def test_lossy_passes_through(downloads: Path, work: Path) -> None:
    files = [
        ff(downloads / "a.mp3", "-c:a", "libmp3lame"),
        ff(downloads / "a.ogg", "-ac", "2", "-c:a", "vorbis", "-strict", "-2"),
        ff(downloads / "aac.m4a", "-c:a", "aac"),
    ]
    for src in files:
        before = src.read_bytes()
        assert await to_flac(src, work) == src
        assert src.read_bytes() == before


async def test_alac_becomes_flac(downloads: Path, work: Path) -> None:
    src = ff(downloads / "alac.m4a", "-c:a", "alac")
    out = await to_flac(src, work)
    assert out.suffix == ".flac" and await _pcm_md5(out) == await _pcm_md5(src)


async def test_tags_and_cover_survive_and_source_is_untouched(downloads: Path, work: Path) -> None:
    src = ff(
        downloads / "a.flac", "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg",
        "-disposition:v", "attached_pic", "-metadata", "title=Song", "-metadata", "artist=Band",
        "-compression_level", "0", cover=True,
    )  # fmt: skip
    before, mtime = src.read_bytes(), src.stat().st_mtime_ns
    out = await to_flac(src, work)
    f = FLAC(out)
    assert f["title"] == ["Song"] and f["artist"] == ["Band"] and len(f.pictures) == 1
    assert (src.read_bytes(), src.stat().st_mtime_ns) == (before, mtime)


async def test_wav_source_is_untouched(downloads: Path, work: Path) -> None:
    src = ff(downloads / "a.wav")
    before, mtime = src.read_bytes(), src.stat().st_mtime_ns
    await to_flac(src, work)
    assert (src.read_bytes(), src.stat().st_mtime_ns) == (before, mtime)


async def test_failed_encode_is_a_permanent_error_with_ffmpeg_output(
    downloads: Path, work: Path
) -> None:
    src = ff(downloads / "a.wav")
    src.write_bytes(src.read_bytes()[:60])  # a header and no audio: still sniffs as WAV
    with pytest.raises(PermanentError, match="ffmpeg"):
        await to_flac(src, work)
