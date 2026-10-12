"""Lossless audio to FLAC with ffmpeg. Never modifies its input; output goes to `work_dir`."""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from mutagen.flac import FLAC

from mixsync.core.errors import PermanentError

log = logging.getLogger(__name__)
_TAIL = 800  # bytes of ffmpeg stderr kept in an error


_MAX_BITS = 24  # the FLAC subset; deeper or float audio cannot be stored losslessly
_PCM_BITS = {"pcm_u8": 8, "pcm_s8": 8}  # others: pcm_s16le, pcm_s24be, ... (bits in the name)


async def _codec_bits(src: Path) -> tuple[str, int] | None:
    """(codec, sample bits; 0 if unknown) of the first audio stream, None if ffprobe cannot
    read audio in `src` (by content)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bits_per_raw_sample",
            "-of", "json", str(src),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe is not installed or not on PATH") from e
    out, _ = await proc.communicate()
    try:
        st = json.loads(out)["streams"][0]
        codec, raw = str(st["codec_name"]), str(st.get("bits_per_raw_sample", ""))
    except (ValueError, KeyError, IndexError):  # not audio ffprobe can read: not ours to convert
        return None
    if m := re.fullmatch(r"pcm_s(\d+)[lb]e", codec):
        bits = int(m.group(1))
    else:
        bits = _PCM_BITS.get(codec) or (
            int(raw) if codec in ("flac", "alac") and raw.isdigit() else 0
        )
    return codec, bits


async def encodable(src: Path) -> str | None:
    """Why `to_flac` would leave `src` as it is (a short reason), or None if it can encode it."""
    if not (cb := await _codec_bits(src)):
        return "not readable audio"
    codec, bits = cb
    if 0 < bits <= _MAX_BITS:
        return None
    if codec.startswith("pcm_f"):
        return f"{codec} is floating point"
    return f"{codec} is {bits}-bit" if bits else f"{codec} is not integer PCM"


async def _probe(src: Path) -> tuple[str, str] | None:
    """(kind, codec) for a file we may encode, else None. kind is "flac" or "other"; only
    integer PCM, FLAC and ALAC up to 24 bits qualify."""
    if not (cb := await _codec_bits(src)) or not 0 < cb[1] <= _MAX_BITS:
        return None
    codec = cb[0]
    return {"flac": "flac", "alac": "alac"}.get(codec, "other"), codec


# ALAC covers come without a picture type, and the MP4 container keys mean nothing in FLAC.
_ALAC_ARGS = (
    "-metadata:s:v", "comment=Cover (front)",
    "-metadata", "major_brand=", "-metadata", "minor_version=", "-metadata", "compatible_brands=",
)  # fmt: skip


async def _ffmpeg(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-xerror",  # a decode error is a failure, not a shorter file
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
    out = await _ffmpeg("-i", str(path), "-map", "0:a", "-c:a", "pcm_s32le", "-f", "md5", "-")
    if not (m := re.search(r"MD5=([0-9a-f]+)", out)):
        raise PermanentError(f"ffmpeg gave no audio checksum for {path}")
    return m.group(1)


def _info(path: Path) -> Any:
    return cast(Any, FLAC(path).info)  # pyright: ignore[reportUnknownMemberType]


_RAW = {8: "pcm_s8", 16: "pcm_s16le", 24: "pcm_s24le"}


async def _flac_intact(src: Path) -> bool:
    """Does the FLAC decode to the audio its STREAMINFO MD5 was taken from? ffmpeg's decoder
    may skip corrupt frames and still exit 0, so this does not rely on its error handling.
    True when there is nothing to compare (no MD5 stored, or a bit depth we do not decode raw)."""
    info = await asyncio.to_thread(_info, src)
    codec = _RAW.get(info.bits_per_sample)
    if not info.md5_signature or codec is None:
        return True
    out = await _ffmpeg("-i", str(src), "-map", "0:a", "-c:a", codec, "-f", "md5", "-")
    m = re.search(r"MD5=([0-9a-f]+)", out)
    return bool(m) and m.group(1) == f"{info.md5_signature:032x}"


async def to_flac(src: Path, work_dir: Path, level: int = 8) -> Path:
    """The file to import in place of `src`: a FLAC in `work_dir` for WAV, AIFF, ALAC and a FLAC
    that shrinks at `level`; `src` itself for lossy files, a FLAC that does not shrink, or an
    encode whose decoded audio differs from the source (logged). A failed encode is a
    PermanentError."""
    if not (probed := await _probe(src)):
        return src
    kind = probed[0]
    if kind == "flac" and not await _flac_intact(src):
        log.warning("%s does not match its own checksum; keeping it as downloaded", src)
        return src
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    out = work_dir / f"{src.stem}.flac"
    if out == src:  # a work dir that holds the source would be overwritten by -y
        raise ValueError(f"{src} is inside the work dir {work_dir}")
    await _ffmpeg(
        "-y", "-i", str(src),
        "-map", "0:a", "-map", "0:v?", "-map_metadata", "0",
        "-c:a", "flac", "-compression_level", str(level), "-c:v", "copy",
        "-disposition:v", "attached_pic", *(_ALAC_ARGS if kind == "alac" else ()), str(out),
    )  # fmt: skip
    same_length = (
        kind != "flac"
        or (await asyncio.to_thread(_info, src)).total_samples
        == (await asyncio.to_thread(_info, out)).total_samples
    )
    if not same_length or await _pcm_md5(src) != await _pcm_md5(out):
        log.warning("re-encoding %s changed the audio; keeping the original", src)
    elif kind != "flac" or (out.stat().st_size < src.stat().st_size):  # noqa: ASYNC240
        return out
    return src  # a rejected `out` stays in work_dir for the caller to clear
