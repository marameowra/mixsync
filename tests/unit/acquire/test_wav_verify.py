"""WAV and AIFF downloads: verify reads a FLAC conversion made in a scratch dir, import still
starts from the untouched download."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from mixsync.core.jobs import JobKind, JobState

from .conftest import Env
from .test_pipeline import decision_rows, needs_ffmpeg, request, status

pytestmark = needs_ffmpeg


def pcm_album(env: Env, ext: str, codec: str = "pcm_s16le") -> dict[str, Path]:
    """Offer the album as WAV/AIFF files (converted from the FLAC fixtures). Returns the path
    each file will have in downloads."""
    cand = env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ext="flac")
    key = (cand.username, cand.folder)
    blobs: dict[str, bytes] = {}
    for name, data in env.source.blobs[key].items():
        src, out = env.source.scratch / name, env.source.scratch / f"{Path(name).stem}.{ext}"
        src.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-map_metadata", "0", "-write_id3v2", "1",
             "-c:a", codec, str(out)],
            check=True,
        )  # fmt: skip
        blobs[out.name] = out.read_bytes()
    env.source.blobs[key] = blobs
    env.source.catalog["alpha album"] = [
        replace(
            cand,
            files=tuple(
                replace(
                    f,
                    path=(p := f.path.rsplit(".", 1)[0] + f".{ext}"),
                    extension=ext,
                    size=len(blobs[p.rsplit("\\", 1)[-1]]),
                )
                for f in cand.files
            ),
        )
    ]
    env.pipeline = replace(env.pipeline, reencode=True)
    env.handlers = env.pipeline.handlers()
    return {n: env.downloads / "Alpha Album" / n for n in blobs}


def snapshot(paths: dict[str, Path]) -> dict[str, tuple[bytes, int]]:
    return {n: (p.read_bytes(), p.stat().st_mtime_ns) for n, p in paths.items()}


@pytest.mark.parametrize(("ext", "codec"), [("wav", "pcm_s16le"), ("aiff", "pcm_s16be")])
async def test_wav_and_aiff_verify_like_flac_and_import_as_flac(
    env: Env, ext: str, codec: str
) -> None:
    paths = pcm_album(env, ext, codec)
    rid = request(env)
    await env.drain(until=lambda: bool(decision_rows(env, 2)))

    assert [d.status for d in decision_rows(env, 2)] == ["auto_accepted"]
    offered = env.source.blobs[("peer1", "Music\\Alpha Band\\Alpha Album")]
    assert {n: p.read_bytes() for n, p in paths.items()} == offered  # downloads untouched
    assert list((env.data_dir / ".work" / "verify").iterdir()) == []  # scratch is gone

    await env.drain()
    assert status(env, rid) == "imported"
    lib = env.data_dir / "library"
    assert sorted(p.name for p in lib.rglob("*.flac")) == [
        "01-01 Song One.flac",
        "01-02 Song Two.flac",
    ]


async def test_downloads_are_byte_and_mtime_unchanged_by_verify(env: Env) -> None:
    paths = pcm_album(env, "wav")
    request(env)
    await env.drain(until=lambda: all(p.exists() for p in paths.values()))
    before = snapshot(paths)
    await env.drain(until=lambda: bool(decision_rows(env, 2)))
    assert snapshot(paths) == before


async def test_a_non_audio_file_named_wav_fails_verify_clearly(env: Env) -> None:
    pcm_album(env, "wav")
    blobs = env.source.blobs[("peer1", "Music\\Alpha Band\\Alpha Album")]
    blobs["02 Song Two.wav"] = bytes(len(blobs["02 Song Two.wav"]))  # same size: it still downloads
    request(env)
    await env.drain(cap=40)

    err = env.jobs(JobKind.VERIFY)[0]
    assert err.status is JobState.FAILED
    assert "unsupported audio" in (err.last_error or "")
    assert "02 Song Two.wav" in (err.last_error or "")
    assert list((env.data_dir / ".work" / "verify").iterdir()) == []


async def test_a_32_bit_wav_fails_verify_clearly(env: Env) -> None:
    pcm_album(env, "wav", "pcm_s32le")
    request(env)
    await env.drain(cap=40)

    err = env.jobs(JobKind.VERIFY)[0]
    assert err.status is JobState.FAILED
    assert "unsupported audio" in (err.last_error or "")
    assert "01 Song One.wav" in (err.last_error or "")
