import subprocess
from pathlib import Path

import pytest

from mixsync.core.matching import FileTrack, TrackInfo
from mixsync.library import measure
from mixsync.match.vetoes import find_vetoes


def ff(out: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True)
    return out


@pytest.fixture
def noise(tmp_path: Path) -> Path:
    return ff(
        tmp_path / "noise.flac",
        *("-f", "lavfi", "-i", "anoisesrc=d=3:c=white:r=44100:a=0.3", "-ac", "2"),
    )


@pytest.fixture
def from_mp3(tmp_path: Path, noise: Path) -> Path:
    mp3 = ff(tmp_path / "noise.mp3", "-i", str(noise), "-b:a", "48k")
    return ff(tmp_path / "from_mp3.flac", "-i", str(mp3))


def vetoes(length: float, high: float | None, tail: float | None) -> tuple[str, ...]:
    f = FileTrack("Song", "a", length=length, high_band=high, trailing_silence=tail)
    return find_vetoes([(f, TrackInfo(title="Song", length=length))])


async def test_full_band_flac_is_not_a_transcode_but_one_from_mp3_is(
    noise: Path, from_mp3: Path
) -> None:
    real, fake = await measure.high_band_share(noise), await measure.high_band_share(from_mp3)
    assert real is not None and fake is not None
    assert vetoes(3, real, None) == ()
    assert vetoes(3, fake, None) == ("'Song': looks transcoded: almost nothing above 16 kHz",)


async def test_lossy_files_are_never_checked_for_transcode(tmp_path: Path, noise: Path) -> None:
    mp3 = ff(tmp_path / "n.mp3", "-i", str(noise), "-b:a", "48k")
    assert await measure.high_band_share(mp3) is None


async def test_tone_padded_with_silence_is_a_padded_preview(tmp_path: Path) -> None:
    padded = ff(
        tmp_path / "padded.flac",
        *("-f", "lavfi", "-i", "sine=f=440:d=30:r=44100", "-af", "apad=whole_dur=240"),
    )
    tail = await measure.trailing_silence(padded, 240.0)
    assert tail is not None and 209 < tail < 211
    assert vetoes(240.0, None, tail) == ("'Song': silent from 0:30 to the end (4:00)",)


async def test_short_natural_fade_out_is_not_a_padded_preview(tmp_path: Path) -> None:
    faded = ff(
        tmp_path / "fade.flac",
        *("-f", "lavfi", "-i", "sine=f=440:d=10:r=44100", "-af", "afade=t=out:st=8.5:d=1.5"),
    )
    tail = await measure.trailing_silence(faded, 10.0)
    assert tail is not None and tail < 1
    assert vetoes(10.0, None, tail) == ()


async def test_a_file_ffmpeg_cannot_read_measures_as_none(tmp_path: Path) -> None:
    junk = tmp_path / "junk.flac"
    junk.write_bytes(b"not audio")
    high, tail = await measure.high_band_share(junk), await measure.trailing_silence(junk, 10.0)
    assert high is None and tail is None
    assert vetoes(10.0, high, tail) == ()
