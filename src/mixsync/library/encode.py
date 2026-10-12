"""Lossless audio to FLAC with ffmpeg. Never modifies its input; output goes to `work_dir`."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, cast

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

from mixsync.core.errors import PermanentError

log = logging.getLogger(__name__)
_TAIL = 800  # bytes of ffmpeg stderr kept in an error


def _lossless(src: Path) -> str | None:
    """ "flac" or "other" for a lossless file by content (not extension), None for anything else."""
    try:
        f = cast(Any, mutagen.File(src))  # pyright: ignore[reportUnknownMemberType]
    except Exception:  # unreadable: not ours to convert
        return None
    if isinstance(f, FLAC):
        return "flac"
    if isinstance(f, WAVE | AIFF) or (
        isinstance(f, MP4) and str(cast(Any, f.info).codec).startswith("alac")
    ):
        return "other"
    return None


async def _ffmpeg(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg is not installed or not on PATH") from e
    out, err = await proc.communicate()
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip()[-_TAIL:]
        raise PermanentError(f"ffmpeg failed: {tail}")
    return out.decode(errors="replace")


async def _pcm_md5(path: Path) -> str:
    out = await _ffmpeg("-i", str(path), "-map", "0:a", "-f", "md5", "-")
    if not (m := re.search(r"MD5=([0-9a-f]+)", out)):
        raise PermanentError(f"ffmpeg gave no audio checksum for {path}")
    return m.group(1)


async def to_flac(src: Path, work_dir: Path, level: int = 8) -> Path:
    """The file to import in place of `src`: a FLAC in `work_dir` for WAV, AIFF, ALAC and a FLAC
    that shrinks at `level`; `src` itself for lossy files, a FLAC that does not shrink, or an
    encode whose decoded audio differs from the source (logged). A failed encode is a
    PermanentError."""
    kind = _lossless(src)
    if kind is None:
        return src
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    out = work_dir / f"{src.stem}.flac"
    if out == src:  # a work dir that holds the source would be overwritten by -y
        raise ValueError(f"{src} is inside the work dir {work_dir}")
    await _ffmpeg(
        "-y", "-i", str(src),
        "-map", "0:a", "-map", "0:v?", "-map_metadata", "0",
        "-c:a", "flac", "-compression_level", str(level), "-c:v", "copy",
        "-disposition:v", "attached_pic", str(out),
    )  # fmt: skip
    if await _pcm_md5(src) != await _pcm_md5(out):
        log.warning("re-encoding %s changed the audio; keeping the original", src)
    elif kind == "other" or (out.stat().st_size < src.stat().st_size):  # noqa: ASYNC240
        return out
    return src  # a rejected `out` stays in work_dir for the caller to clear
