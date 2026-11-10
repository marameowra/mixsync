"""A Spotify playlist file as a mirrored playlist: upload, reject, re-upload diff, sync."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.core.jobs import JobKind
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.safety import FileOp
from mixsync.discovery import mirrored
from mixsync.discovery.spotify_file import KIND, SpotifyFile

from .conftest import Env, login
from .test_mirrored_playlists import REL, MirrorRig, own_in_navidrome


def export(name: str, *ns: int, more: tuple[str, list[int]] | None = None) -> bytes:
    def items(ns: list[int] | tuple[int, ...]) -> list[object]:
        return [{"track": {"trackName": f"Upstream {n}", "artistName": "Artist"}} for n in ns]

    lists = [{"name": name, "items": items(ns)}]
    if more:
        lists.append({"name": more[0], "items": items(more[1])})
    return json.dumps({"playlists": lists}).encode()


class FileRig(MirrorRig):
    def __init__(self, env: Env, config: Path) -> None:
        super().__init__(env)
        self.mirror.upstreams[KIND] = SpotifyFile(config / "spotify")

    def upload(
        self, web: TestClient, data: bytes, filename: str = "Playlist1.json", name: str = ""
    ) -> int:
        tok = login(web, "alice")
        r = web.post(
            "/mirrored/spotify",
            data={"csrf_token": tok, "name": name},
            files={"file": (filename, data)},
        )
        return r.status_code

    def mirrors(self) -> list[MirroredPlaylist]:
        with self.env.sessions() as s:
            rows = list(s.scalars(select(MirroredPlaylist)))
            s.expunge_all()
            return rows


@pytest.fixture
def fr(env: Env, tmp_path: Path) -> FileRig:
    return FileRig(env, tmp_path / "config")


async def test_upload_then_reupload_applies_the_diff_and_the_same_file_is_a_no_op(
    fr: FileRig, web: TestClient
) -> None:
    own_in_navidrome(fr, 1, 2, 3)
    assert fr.upload(web, export("Faves", 1, 2)) == 303
    await fr.sync()
    (pl,) = fr.mirrors()
    assert (pl.name, pl.upstream_kind) == ("Faves", KIND)
    assert fr.songs() == ["s1", "s2"]
    assert [e["title"] for e in pl.entries] == ["Upstream 1", "Upstream 2"]

    jobs = len(fr.env.jobs(JobKind.MIRROR_SYNC))
    assert fr.upload(web, export("Faves", 1, 2)) == 303  # same file: nothing queued
    assert len(fr.env.jobs(JobKind.MIRROR_SYNC)) == jobs
    assert len(fr.mirrors()) == 1

    assert fr.upload(web, export("Faves", 3, 1)) == 303  # newer file, within the sync gap
    await fr.sync()
    assert fr.songs() == ["s3", "s1"]
    with fr.env.sessions() as s:
        assert s.scalar(select(FileOp.id)) is None  # a removed track touches no file


async def test_missing_tracks_wait_for_confirmation_and_unresolved_are_listed(
    fr: FileRig, web: TestClient
) -> None:
    fr.resolver.unresolved.add("Upstream 2")
    fr.upload(web, export("Faves", 1, 2))
    await fr.sync()
    assert fr.requests() == []
    page = web.get("/mirrored").text
    assert "1 track will be requested" in page
    assert "Artist - Upstream 2" in page
    assert "unresolved" in page.lower()
    tok = login(web, "alice")
    web.post(f"/mirrored/{fr.mirrors()[0].id}/request", data={"csrf_token": tok, "release": [REL]})
    assert len(fr.requests()) == 1


def test_a_data_export_with_several_playlists_needs_a_name(fr: FileRig, web: TestClient) -> None:
    data = export("One", 1, more=("Two", [2]))
    assert fr.upload(web, data) == 400
    assert fr.mirrors() == []
    assert fr.upload(web, data, name="Nope") == 400
    assert fr.upload(web, data, name="Two") == 303
    assert [p.name for p in fr.mirrors()] == ["Two"]


def test_csv_upload_names_the_playlist_after_the_file(fr: FileRig, web: TestClient) -> None:
    csv_text = b"Track Name,Artist Name(s),ISRC\nUpstream 1,Artist,GBABC1234567\n"
    assert fr.upload(web, csv_text, "Road Trip.csv") == 303
    assert [p.name for p in fr.mirrors()] == ["Road Trip"]


@pytest.mark.parametrize(
    "data", [b"", b"{oops", b'{"playlists": [{"name": "x", "items": []}]}', b"a,b\n1,2\n"]
)
def test_bad_files_are_rejected_and_create_nothing(
    fr: FileRig, web: TestClient, data: bytes, tmp_path: Path
) -> None:
    assert fr.upload(web, data) == 400
    assert fr.mirrors() == []
    assert fr.env.jobs(JobKind.MIRROR_SYNC) == []
    assert not (tmp_path / "config" / "spotify").exists()


def test_a_name_used_by_another_mirror_is_a_conflict(fr: FileRig, web: TestClient) -> None:
    with fr.env.sessions.begin() as s:
        mirrored.create(s, fr.env.clock.now(), fr.env.user_id, "Faves", "fake", "x")
    assert fr.upload(web, export("Faves", 1)) == 409
    assert len(fr.mirrors()) == 1
