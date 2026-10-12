import shutil
import subprocess
from pathlib import Path

import mutagen
import pytest
from mutagen.flac import FLAC

from mixsync.core.errors import PermanentError
from mixsync.library import encode
from mixsync.library.encode import _pcm_md5, to_flac

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

SINE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100"]
COVER = ["-f", "lavfi", "-i", "color=c=red:s=64x64:d=1"]


HI_RES = ["-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*440*t):d=2:s=44100"]  # float: real 24-bit


def ff(out: Path, *args: str, cover: bool = False, sine: list[str] = SINE) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", *sine, *(COVER if cover else []), *args, str(out)]
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


async def test_failed_ffmpeg_is_a_permanent_error_with_its_output(tmp_path: Path) -> None:
    with pytest.raises(PermanentError, match="ffmpeg failed: .*Error opening input"):
        await encode._ffmpeg("-i", str(tmp_path / "nonexistent.wav"), "-f", "null", "-")


async def test_unreadable_audio_passes_through(downloads: Path, work: Path) -> None:
    src = downloads / "a.wav"
    src.write_bytes(b"RIFF not audio")
    assert await to_flac(src, work) == src


async def test_deep_float_and_wrapped_lossy_pass_through_untouched(
    downloads: Path, work: Path
) -> None:
    files = [
        ff(downloads / "i32.wav", "-c:a", "pcm_s32le"),
        ff(downloads / "f32.wav", "-c:a", "pcm_f32le"),
        ff(downloads / "mp3.wav", "-c:a", "libmp3lame", "-f", "wav"),
    ]
    for src in files:
        before = src.read_bytes()
        assert await to_flac(src, work) == src
        assert src.read_bytes() == before


async def test_24_bit_is_encoded(downloads: Path, work: Path) -> None:
    src = ff(downloads / "a.wav", "-c:a", "pcm_s24le")
    out = await to_flac(src, work)
    assert out != src and FLAC(out).info.bits_per_sample == 24


async def test_md5_sees_the_low_bits(downloads: Path) -> None:
    src = ff(downloads / "a.wav", "-c:a", "pcm_s24le", sine=HI_RES)
    cut = ff(downloads / "cut.flac", "-c:a", "flac", "-sample_fmt", "s16", sine=HI_RES)
    assert await _pcm_md5(src) != await _pcm_md5(cut)


async def test_a_24_bit_source_cut_to_16_bits_keeps_the_original(
    downloads: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = encode._ffmpeg

    async def cutting(*args: str) -> str:
        if "-compression_level" in args:
            args = (*args[:-1], "-sample_fmt", "s16", args[-1])
        return await real(*args)

    monkeypatch.setattr(encode, "_ffmpeg", cutting)
    src = ff(downloads / "a.wav", "-c:a", "pcm_s24le", sine=HI_RES)
    assert await to_flac(src, work) == src


async def test_alac_cover_is_a_front_cover_without_container_keys(
    downloads: Path, work: Path
) -> None:
    src = ff(
        downloads / "alac.m4a", "-map", "0:a", "-map", "1:v", "-c:a", "alac", "-c:v", "mjpeg",
        "-disposition:v", "attached_pic", cover=True,
    )  # fmt: skip
    f = FLAC(await to_flac(src, work))
    assert [p.type for p in f.pictures] == [3]
    assert not {"major_brand", "minor_version", "compatible_brands"} & set(f.keys())
