"""WAV and AIFF downloads: verify reads a FLAC conversion made in a scratch dir, import still
starts from the untouched download."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from mutagen.aiff import AIFF
from mutagen.id3 import TALB, TIT2, TPE1, TRCK, TXXX, UFID
from mutagen.wave import WAVE

from mixsync.acquire import pipeline
from mixsync.acquire.pipeline import file_track, likelies
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.work import Request
from mixsync.library import tags

from .conftest import Env
from .fakes import ALBUM, AUDIO
from .test_pipeline import decision_rows, needs_ffmpeg, request, status


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
            ["ffmpeg", "-v", "error", "-i", str(src), "-c:a", codec, str(out)], check=True
        )
        t = tags.read_fields(src)  # WAV/AIFF carry ID3, as Picard writes them
        tags.write(
            out,
            tags.TagSet(
                t["title"], t["artist"], t["album"], t["albumartist"], int(t["tracknumber"]), 1
            ),
        )
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


@needs_ffmpeg
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

    await env.drain()
    assert status(env, rid) == "imported"
    lib = env.data_dir / "library"
    assert sorted(p.name for p in lib.rglob("*.flac")) == [
        "01-01 Song One.flac",
        "01-02 Song Two.flac",
    ]


@needs_ffmpeg
async def test_downloads_are_byte_and_mtime_unchanged_by_verify(env: Env) -> None:
    paths = pcm_album(env, "wav")
    request(env)
    await env.drain(until=lambda: all(p.exists() for p in paths.values()))
    before = snapshot(paths)
    await env.drain(until=lambda: bool(decision_rows(env, 2)))
    assert snapshot(paths) == before


async def refused(env: Env) -> Request:
    """Run until the request leaves verify; the next peer must be tried, not the request failed."""
    rid = request(env)
    await env.drain(
        until=lambda: bool(env.jobs(JobKind.VERIFY)) and status(env, rid) != "verifying"
    )
    await env.drain(until=lambda: status(env, rid) == "searching")
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req
        return req


@needs_ffmpeg
async def test_a_non_audio_file_named_wav_is_refused_and_the_next_peer_tried(env: Env) -> None:
    paths = pcm_album(env, "wav")
    blobs = env.source.blobs[("peer1", "Music\\Alpha Band\\Alpha Album")]
    blobs["02 Song Two.wav"] = bytes(len(blobs["02 Song Two.wav"]))  # same size: still downloads
    req = await refused(env)

    assert req.tried == [["peer1", "Music\\Alpha Band\\Alpha Album"]]
    assert "unsupported audio: 02 Song Two.wav" in (req.last_error or "")
    assert env.jobs(JobKind.VERIFY)[0].status is JobState.SUCCEEDED
    assert all(p.exists() for p in paths.values())


@pytest.mark.parametrize("codec", ["pcm_s32le", "pcm_f32le"])
@needs_ffmpeg
async def test_32_bit_and_float_wav_are_refused_and_the_next_peer_tried(
    env: Env, codec: str
) -> None:
    pcm_album(env, "wav", codec)
    req = await refused(env)

    assert req.tried == [["peer1", "Music\\Alpha Band\\Alpha Album"]]
    assert f"unsupported audio: 01 Song One.wav: {codec} is" in (req.last_error or "")


def picard_wav(path: Path, ext: str) -> Path:
    out = path.with_suffix(f".{ext}")
    codec = "pcm_s16be" if ext == "aiff" else "pcm_s16le"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(AUDIO / "cc0-tone-01.flac"), "-c:a", codec,
         str(out)],
        check=True,
    )  # fmt: skip
    f = WAVE(out) if ext == "wav" else AIFF(out)
    f.add_tags()
    assert f.tags is not None
    f.tags.add(TIT2(encoding=3, text="Song One"))
    f.tags.add(TPE1(encoding=3, text="Alpha Band"))
    f.tags.add(TALB(encoding=3, text="Alpha Album"))
    f.tags.add(TRCK(encoding=3, text="1/2"))
    f.tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=ALBUM.release_id))
    f.tags.add(UFID(owner="http://musicbrainz.org", data=b"rec-1"))
    f.save()
    return out


@pytest.mark.parametrize("ext", ["wav", "aiff"])
def test_picard_tagged_wav_and_aiff_keep_their_musicbrainz_ids(tmp_path: Path, ext: str) -> None:
    path = picard_wav(tmp_path / "01 Song One", ext)
    before = path.read_bytes()

    ft = file_track(path, 1.0, ())
    like = likelies([path])

    assert (ft.title, ft.artist, ft.track, ft.recording_id) == (
        "Song One",
        "Alpha Band",
        1,
        "rec-1",
    )
    assert like.release_id == ALBUM.release_id  # a migration with no release_mbid identifies by it
    assert path.read_bytes() == before


async def test_readable_formats_never_reach_ffprobe_or_to_flac(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: object) -> object:
        raise AssertionError("must not be called for FLAC/MP3")

    monkeypatch.setattr(pipeline, "encodable", boom)
    monkeypatch.setattr(pipeline, "to_flac", boom)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    rid = request(env)
    await env.drain(until=lambda: bool(decision_rows(env, 2)))
    assert [d.status for d in decision_rows(env, 2)] == ["auto_accepted"]
    assert status(env, rid) == "importing"
