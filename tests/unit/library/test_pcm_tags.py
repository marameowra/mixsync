"""Tagging WAV/AIFF (only when imported unencoded) must never touch the audio."""

import shutil
import subprocess
from pathlib import Path

import pytest

from mixsync.library import tags
from mixsync.library.encode import _pcm_md5  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

CASES = [
    ("a.wav", "pcm_s16le", "2"),
    ("a.wav", "pcm_s24le", "2"),
    ("a.wav", "pcm_u8", "1"),
    ("a.aiff", "pcm_s16be", "2"),
    ("a.aiff", "pcm_s24be", "2"),
]


def generate(path: Path, codec: str, channels: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.0101:sample_rate=44100",
         "-ac", channels, "-c:a", codec, str(path)],
        check=True,
    )  # fmt: skip  # an odd number of samples: the chunk needs a pad byte


@pytest.mark.parametrize(("name", "codec", "channels"), CASES)
async def test_tag_write_and_restore_keep_the_audio_bit_identical(
    tmp_path: Path, name: str, codec: str, channels: str
) -> None:
    path = tmp_path / name
    generate(path, codec, channels)
    audio = await _pcm_md5(path)
    original = tags.read_all(path)

    full = tags.TagSet(
        "Song", "Artist", "Album", "Artist", 3, 1, 2020,
        recording_id="11111111-1111-1111-1111-111111111111",
        release_id="22222222-2222-2222-2222-222222222222",
    )  # fmt: skip
    tags.write(path, full)
    assert await _pcm_md5(path) == audio
    assert tags.read_fields(path)["musicbrainz_albumid"] == full.release_id

    tags.write(path, tags.TagSet("T" * 5000, "Artist", "Album", "Artist", 3, 1))
    assert await _pcm_md5(path) == audio
    assert tags.read_fields(path)["title"] == "T" * 5000

    tags.restore(path, original)
    assert await _pcm_md5(path) == audio
    assert tags.read_all(path)["tags"] == original["tags"]
