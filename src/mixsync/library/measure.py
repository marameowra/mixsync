"""Read-only audio measurements with ffmpeg, for the VERIFY vetoes (see match/vetoes.py).

Never writes or modifies the file. A measurement ffmpeg cannot make is None, never an error.
"""

import asyncio
import logging
import re
from pathlib import Path

import mutagen

from mixsync.match.candidates import LOSSLESS

log = logging.getLogger(__name__)

HIGH_BAND_HZ = 16000
SILENCE_DB = -50  # below this counts as silence
SILENCE_MIN = 1.0  # seconds; a shorter pause is not reported by silencedetect
_END_SLACK = 0.5  # a silence ending this close to the end of the file runs to the end
_MIN_RATE = 40000  # below this the file cannot hold 16 kHz (Nyquist)
_RMS = re.compile(r"Overall\s+\[[^\]]*\]\s+RMS level dB: (\S+)")


async def _ffmpeg_log(path: Path, af: str) -> str | None:
    """ffmpeg's log for decoding `path` through the audio filter `af`; None if it failed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-map", "0:a:0",
            "-af", af, "-f", "null", "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except FileNotFoundError:
        return None
    _, err = await proc.communicate()
    return err.decode(errors="replace") if proc.returncode == 0 else None


async def _rms_db(path: Path, prefix: str) -> float | None:
    out = await _ffmpeg_log(
        path, f"{prefix}astats=measure_perchannel=none:measure_overall=RMS_level"
    )
    m = out and _RMS.search(out.replace("\n", " "))
    try:
        db = float(m.group(1)) if m else None
    except ValueError:
        return None
    return db if db is not None and db > float("-inf") else None


async def high_band_share(path: Path) -> float | None:
    """Share (0..1) of the signal's power above HIGH_BAND_HZ, for lossless files only."""
    if path.suffix.lower().lstrip(".") not in LOSSLESS:
        return None
    try:
        info = mutagen.File(path)  # pyright: ignore[reportUnknownMemberType]
        if info is None or info.info.sample_rate < _MIN_RATE:
            return None
        # two passes of the 2-pole filter: steeper, so music below 16 kHz leaks less in
        high = await _rms_db(path, f"highpass=f={HIGH_BAND_HZ},highpass=f={HIGH_BAND_HZ},")
        full = await _rms_db(path, "")
    except Exception:
        log.warning("high-band measurement of %s failed", path, exc_info=True)
        return None
    return None if high is None or full is None else min(1.0, 10 ** ((high - full) / 10))


async def trailing_silence(path: Path, duration: float) -> float | None:
    """Seconds of silence running to the end of the `duration`-long file (0.0 if none)."""
    try:
        out = await _ffmpeg_log(path, f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN}")
    except Exception:
        log.warning("silence measurement of %s failed", path, exc_info=True)
        return None
    if out is None:
        return None
    starts = re.findall(r"silence_start: (\S+)", out)
    if not starts:
        return 0.0
    ends = re.findall(r"silence_end: (\S+)", out)
    start = float(starts[-1])
    # at end of input ffmpeg closes an open silence, so the last end tells if it ran to the end
    if len(ends) >= len(starts) and float(ends[-1]) < duration - _END_SLACK:
        return 0.0
    return max(0.0, duration - start)
