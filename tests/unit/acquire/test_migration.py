"""Migration re-import: preflight, the identify-and-import flow, the old-tree check and the page."""

import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from mixsync.acquire import migration as mig
from mixsync.acquire import pipeline, verify
from mixsync.acquire.migration import Migrator
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Band, MatchResult
from mixsync.core.types import AcoustIdResult, ReleaseRef
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Migration, Request
from mixsync.db.queue import enqueue_in
from mixsync.library import tags
from mixsync.match import scorer

from .conftest import Env, login
from .fakes import ALBUM, ALBUM_B, AUDIO, CATALOG, OTHER, RECORDINGS, FakeMetadata
from .test_pipeline import decision_rows, library


class Meta(FakeMetadata):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[ReleaseRef] = []
        self.searches: list[tuple[str, str, int]] = []

    async def search_releases(self, artist: str, album: str, limit: int = 10) -> list[ReleaseRef]:
        self.searches.append((artist, album, limit))
        return self.refs


def ref(album: Any) -> ReleaseRef:
    return ReleaseRef(album.release_id, album.album, album.artist)


def put_album(root: Path, rel: str, album: Any = ALBUM, mbid: str | None = None) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for t in album.tracks:
        p = d / f"{t.index:02} {t.title}.mp3"
        shutil.copy(AUDIO / "cc0-tone-01.mp3", p)
        tags.write(
            p,
            tags.TagSet(
                t.title,
                album.artist,
                album.album,
                album.artist,
                t.index,
                1,
                album.year,
                release_id=mbid,
            ),
        )
    return d


def manifest_lines(m: Migration) -> list[str]:
    return Path(m.manifest_path).read_text().splitlines()


class Rig:
    def __init__(self, env: Env, tmp_path: Path) -> None:
        self.env, self.root = env, tmp_path / "old"
        self.root.mkdir()
        self.meta = Meta()
        env.pipeline = replace(env.pipeline, metadata=self.meta)
        self.migrator = Migrator(
            env.sessions, env.clock, self.root, tmp_path / "config", env.data_dir / "library", 1.0
        )
        env.handlers = env.pipeline.handlers() | self.migrator.handlers()
        with env.sessions.begin() as s:
            self.admin = create_user(s, "root", "pw", admin=True).id

    async def preflight(self) -> int:
        with self.env.sessions.begin() as s:
            mid = mig.start_preflight(s, self.env.clock.now(), self.admin).id
        await self.env.drain()
        return mid

    def get(self, mid: int) -> Migration:
        with self.env.sessions() as s:
            m = s.get(Migration, mid)
            assert m
            s.expunge(m)
            return m

    def confirm(self, mid: int) -> int:
        with self.env.sessions.begin() as s:
            m = s.get(Migration, mid)
            assert m
            return mig.confirm(s, self.env.clock.now(), m)

    def requests(self) -> list[Request]:
        with self.env.sessions() as s:
            return list(s.scalars(select(Request).order_by(Request.id)))

    def tree(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def rig(env: Env, tmp_path: Path) -> Rig:
    return Rig(env, tmp_path)


async def test_preflight_groups_manifest_and_eta(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", ALBUM, mbid=str(ALBUM.release_id))
    put_album(rig.root, "Beta Band/Beta Album/CD1", OTHER)  # disc folders: one album
    cd2 = rig.root / "Beta Band/Beta Album/CD2"
    cd2.mkdir()
    (rig.root / "Beta Band/Beta Album/CD1/02 Tune Two.mp3").rename(cd2 / "02 Tune Two.mp3")
    put_album(rig.root, "loose", ALBUM)  # two albums in one folder
    put_album(rig.root, "loose", OTHER)
    (rig.root / "Alpha Band/Alpha Album/cover.jpg").write_bytes(b"jpg")
    (rig.root / "odd\tname\n.cue").write_text("cue")
    (rig.root / "junk.mp3").write_text("not audio")
    locked = put_album(rig.root, "locked", ALBUM) / "01 Song One.mp3"
    real = mig._hash  # pyright: ignore[reportPrivateUsage]

    def hash_(p: Path) -> tuple[int, str]:
        if p == locked:
            raise PermissionError(13, "Permission denied")
        return real(p)

    monkeypatch.setattr(mig, "_hash", hash_)

    m = rig.get(await rig.preflight())
    sm = m.summary
    assert m.status == "ready", sm
    assert sm["files"] == 12 and sm["albums"] == 5 and sm["tagged_albums"] == 1
    assert sorted(u["path"] for u in sm["unreadable"]) == [
        "junk.mp3",
        "locked/01 Song One.mp3",
    ]
    groups = {Path(g.folder).relative_to(rig.root).as_posix(): g for g in mig.groups_of(m)}
    beta = groups["Beta Band/Beta Album"]
    assert sorted(f for f, _ in beta.files) == ["CD1/01 Tune One.mp3", "CD2/02 Tune Two.mp3"]
    assert groups["Alpha Band/Alpha Album"].mbid == ALBUM.release_id
    assert len(groups["Alpha Band/Alpha Album"].files) == 2  # cover.jpg is not audio
    assert sorted(g.album for g in mig.groups_of(m) if g.folder.endswith("loose")) == [
        "Alpha Album",
        "Beta Album",
    ]
    manifest = {e["path"]: e for e in map(json.loads, manifest_lines(m))}
    one = rig.root / "Alpha Band/Alpha Album/01 Song One.mp3"
    assert manifest["Alpha Band/Alpha Album/01 Song One.mp3"] == {
        "path": "Alpha Band/Alpha Album/01 Song One.mp3",
        "size": one.stat().st_size,
        "sha256": hashlib.sha256(one.read_bytes()).hexdigest(),
    }
    assert "locked/01 Song One.mp3" not in manifest and "junk.mp3" in manifest
    assert "Alpha Band/Alpha Album/cover.jpg" in manifest and "odd\tname\n.cue" in manifest
    # 1 tagged album at 1 request, 4 untagged at 4, one MB request per second; 9 tracks at 3/s
    assert sm["eta_seconds"] == pytest.approx(17 + 9 / 3)


async def test_preflight_refuses_without_free_space(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "A/B")
    size = sum(p.stat().st_size for p in rig.root.rglob("*.mp3"))
    monkeypatch.setattr(
        mig.shutil, "disk_usage", lambda _p: SimpleNamespace(free=int(size * 1.1) - 1)
    )
    m = rig.get(await rig.preflight())
    assert m.status == "failed" and "needed" in m.summary["reason"]
    assert rig.confirm(m.id) == 0 and rig.requests() == []

    monkeypatch.setattr(mig.shutil, "disk_usage", lambda _p: SimpleNamespace(free=int(size * 1.1)))
    assert rig.get(await rig.preflight()).status == "ready"


async def test_confirm_makes_low_priority_jobs_once(rig: Rig) -> None:
    put_album(rig.root, "A/One")
    put_album(rig.root, "B/Two", OTHER)
    mid = await rig.preflight()
    assert rig.confirm(mid) == 2 and rig.confirm(mid) == 0  # the second does nothing
    reqs = rig.requests()
    assert [(r.status, r.release_mbid, r.migration_id) for r in reqs] == [
        ("verifying", "", mid)
    ] * 2
    assert {r.album for r in reqs} == {"Alpha Album", "Beta Album"}
    jobs = rig.env.jobs(JobKind.VERIFY)
    assert len(jobs) == 2 and {j.priority for j in jobs} == {1}
    p = jobs[0].payload
    assert p["release_mbid"] is None and p["candidate"]["username"] == ""
    assert p["candidate"]["files"][0]["extension"] == "mp3"
    assert all(Path(x).is_absolute() for x in p["paths"])
    assert [j.idempotency_key for j in jobs] == [f"migrate:{mid}:0", f"migrate:{mid}:1"]
    assert all(j.max_attempts == verify.VERIFY_ATTEMPTS for j in jobs)
    await rig.env.drain()
    assert [d.release_given for d in decision_rows(rig.env, 2)] == [False, False]  # identified


async def test_tagged_album_imports_without_search_and_leaves_the_source_alone(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    before = rig.tree()

    def boom(*_a: Any) -> None:
        raise AssertionError("the source must not be released")

    monkeypatch.setattr(rig.env.pipeline.importer.fileops, "release", boom)
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()

    (req,) = rig.requests()
    assert req.status == "imported" and req.release_mbid == ALBUM.release_id
    assert (req.artist, req.album) == ("Alpha Band", "Alpha Album")
    assert rig.meta.searches == [] and rig.env.source.cleared == []
    with rig.env.sessions() as s:  # a complete import is not "partial"
        assert [r["status"] for r in mig.report(s, s.get_one(Migration, mid))] == ["imported"]
    assert len(library(rig.env)) == 2
    assert rig.tree() == before  # byte-identical
    assert [d.band for d in decision_rows(rig.env, 2)] == ["auto_accept"]
    assert rig.env.jobs(JobKind.SEARCH) == []


def put_tagged(root: Path, rel: str, ext: str, album: Any = ALBUM) -> Path:
    """The album as `ext` files, converted from the mp3 fixture with ffmpeg, then tagged."""
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for t in album.tracks:
        p = d / f"{t.index:02} {t.title}.{ext}"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(AUDIO / "cc0-tone-01.mp3"), str(p)], check=True
        )
        rid = str(album.release_id)
        tags.write(
            p,
            tags.TagSet(
                t.title, album.artist, album.album, album.artist, t.index, 1, release_id=rid
            ),
        )
    return d


@pytest.mark.parametrize("ext", ["opus", "wav", "aiff"])
async def test_opus_and_wav_albums_are_grouped_and_imported_as_is_or_as_flac(
    rig: Rig, ext: str
) -> None:
    put_tagged(rig.root, "Alpha Band/Alpha Album", ext)
    before = rig.tree()
    mid = await rig.preflight()
    sm = rig.get(mid).summary
    assert sm["unreadable"] == [] and sm["albums"] == 1
    (group,) = mig.groups_of(rig.get(mid))
    assert sorted(f for f, _ in group.files) == [f"01 Song One.{ext}", f"02 Song Two.{ext}"]
    rig.env.pipeline = replace(rig.env.pipeline, reencode=True)
    rig.env.handlers = rig.env.pipeline.handlers() | rig.migrator.handlers()
    rig.confirm(mid)
    await rig.env.drain()
    (req,) = rig.requests()
    assert req.status == "imported", req.last_error
    lib = rig.env.data_dir / "library"
    want = "opus" if ext == "opus" else "flac"  # WAV/AIFF are encoded to FLAC; the original stays
    assert len(list(lib.rglob(f"*.{want}"))) == 2
    assert [tags.read_fields(p)["album"] for p in lib.rglob(f"*.{want}")] == ["Alpha Album"] * 2
    assert rig.tree() == before  # byte-identical, WAV originals included


async def test_unsupported_audio_is_listed_in_preflight_and_skipped_in_the_report(
    rig: Rig,
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    (rig.root / "Alpha Band/Alpha Album/03 Old.WMA").write_bytes(b"wma")
    mid = await rig.preflight()
    m = rig.get(mid)
    assert m.summary["unreadable"] == [
        {"path": "Alpha Band/Alpha Album/03 Old.WMA", "why": "unsupported format"}
    ]
    with rig.env.sessions() as s:
        (row,) = mig.report(s, m)
    assert row["status"] == "skipped" and row["reason"] == "03 Old.WMA: unsupported format"


async def test_stale_tagged_id_falls_back_to_search(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid="99999999-9999-9999-9999-999999999999")
    rig.meta.refs = [ref(ALBUM)]
    real = rig.meta.get_release

    async def get_release(mbid: str) -> Any:
        if mbid.startswith("9999"):
            raise PermanentError("returned 404")
        return await real(mbid)

    rig.meta.get_release = get_release  # type: ignore[method-assign]
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    assert rig.meta.searches == [("Alpha Band", "Alpha Album", 5)]
    # the stale tag still disagrees with the release found, so a human looks at it
    assert [(r.status, r.release_mbid) for r in rig.requests()] == [("review", ALBUM.release_id)]


async def test_transient_error_on_tagged_id_does_not_search(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))

    async def down(_mbid: str) -> Any:
        raise TransientError("musicbrainz is down")

    rig.meta.get_release = down  # type: ignore[method-assign]
    rig.confirm(await rig.preflight())
    await rig.env.drain(until=lambda: bool(rig.env.jobs(JobKind.VERIFY)[0].attempts))
    assert rig.env.jobs(JobKind.VERIFY)[0].status is JobState.RETRY_WAIT
    assert rig.meta.searches == []


async def test_untagged_album_picks_the_best_of_the_top_three(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.meta.refs = [ref(OTHER), ref(ALBUM), ref(OTHER), ref(OTHER)]
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    assert rig.meta.searches == [("Alpha Band", "Alpha Album", 5)]
    assert rig.meta.calls[:3] == [OTHER.release_id, ALBUM.release_id, OTHER.release_id]
    (req,) = rig.requests()
    assert req.status == "imported" and req.release_mbid == ALBUM.release_id


async def test_two_near_identical_releases_go_to_review_with_the_runner_up(
    rig: Rig, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    twin = replace(ALBUM_B, year=ALBUM.year)  # same group, tracks and year: only the id differs
    monkeypatch.setitem(CATALOG, str(twin.release_id), twin)
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.meta.refs = [ref(ALBUM), ref(twin)]
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert (req.status, d.status) == ("review", "pending_review")
    assert d.evidence["runner_up"]["release_id"] == twin.release_id
    login(web, "root")
    assert "Close runner-up" in web.get(f"/review/{d.id}").text


async def test_no_candidate_goes_to_review_and_reject_skips(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert req.status == "review" and (d.band, d.status, d.release_mbid) == (
        "review",
        "pending_review",
        "",
    )
    files = d.evidence["source"]["files"]
    assert all(f["local_path"].startswith(str(rig.root)) for f in files) and len(files) == 2

    tok = login(web, "root")
    assert "Mark unverified" in web.get(f"/review/{d.id}").text
    assert web.post(f"/review/{d.id}/reject", data={"csrf_token": tok}).status_code == 303
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.last_error) == ("skipped", "rejected in review")
    assert rig.env.jobs(JobKind.SEARCH) == [] and rig.env.source.queries == []
    assert rig.env.source.cleared == [] and len(rig.tree()) == 2


async def test_reject_band_goes_to_review_not_to_search(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.meta.refs = [ref(OTHER)]  # the wrong album is all MusicBrainz offers
    real = pipeline.score_files
    reject = lambda *a: replace(real(*a), band=Band.REJECT, distance=0.9)  # noqa: E731
    monkeypatch.setattr(pipeline, "score_files", reject)
    monkeypatch.setattr(scorer, "score_files", reject)  # the release choice scores there
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert d.distance == 0.9  # the scorer rejected it
    assert (req.status, d.status) == ("review", "pending_review")
    assert rig.env.jobs(JobKind.SEARCH) == []


async def test_mark_unverified_copies_and_leaves_the_source(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    before = rig.tree()
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    (d,) = decision_rows(rig.env, 2)
    tok = login(web, "root")
    form = {
        "albumartist": "Alpha Band",
        "album": "Alpha Album",
        "title": ["Song One", "Song Two"],
        "artist": ["", ""],
        "disc": ["1", "1"],
        "track": ["1", "2"],
        "csrf_token": tok,
    }
    assert web.post(f"/review/{d.id}/unverified", data=form).status_code == 303
    await rig.env.drain()
    assert rig.requests()[0].status == "unverified" and len(library(rig.env)) == 2
    assert rig.tree() == before and rig.env.source.cleared == []


async def test_acoustid_outage_retries_a_migrated_album(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    rig.confirm(await rig.preflight())
    rig.env.acoustid.fail = 1
    await rig.env.drain(until=lambda: rig.env.acoustid.fail == 0)
    first, again = rig.env.jobs(JobKind.VERIFY)
    assert first.status is JobState.SUCCEEDED and again.status is JobState.QUEUED
    assert decision_rows(rig.env, 2) == []
    await rig.env.drain()
    assert rig.requests()[0].status == "imported"


async def test_migration_verify_runs_after_normal_requests(rig: Rig) -> None:
    put_album(rig.root, "A/B")
    rig.confirm(await rig.preflight())
    with rig.env.sessions.begin() as s:
        enqueue_in(s, rig.env.clock.now(), JobKind.NOOP, {})
    job = rig.env.queue.claim("w")
    assert job and job.kind is JobKind.NOOP


async def check(rig: Rig, mid: int) -> dict[str, Any]:
    with rig.env.sessions.begin() as s:
        enqueue_in(s, rig.env.clock.now(), JobKind.MIGRATE_CHECK, {"migration_id": mid})
    await rig.env.drain()
    verify = rig.get(mid).verify
    assert verify
    return verify


async def test_check_job_detects_changes(rig: Rig) -> None:
    put_album(rig.root, "A/One")
    put_album(rig.root, "B/Two", OTHER)
    (rig.root / "A/One/cover.jpg").write_bytes(b"jpg")
    mid = await rig.preflight()
    assert (await check(rig, mid))["identical"] is True

    with (rig.root / "A/One/01 Song One.mp3").open("ab") as f:
        f.write(b"x")
    (rig.root / "B/Two/01 Tune One.mp3").unlink()
    shutil.copy(AUDIO / "cc0-tone-01.mp3", rig.root / "B/Two/03 New.mp3")
    (rig.root / "A/One/cover.jpg").write_bytes(b"jpg2")  # not audio, still counted
    result = await check(rig, mid)
    assert result["identical"] is False
    assert result["changed"] == ["A/One/01 Song One.mp3", "A/One/cover.jpg"]
    assert result["missing"] == ["B/Two/01 Tune One.mp3"]
    assert result["added"] == ["B/Two/03 New.mp3"]
    assert result["counts"] == {"changed": 2, "missing": 1, "added": 1}


def test_page_is_admin_only_and_confirm_is_guarded(rig: Rig, web: TestClient) -> None:
    assert web.get("/migration").status_code == 303
    tok = login(web, "alice")
    assert web.get("/migration").status_code == 403
    assert web.post("/migration/preflight", data={"csrf_token": tok}).status_code == 403
    assert "/migration" not in web.get("/").text

    tok = login(web, "root")
    assert 'href="/migration"' in web.get("/").text
    assert "Start preflight" in web.get("/migration").text
    assert web.post("/migration/preflight", data={"csrf_token": tok}).status_code == 303
    assert "Start preflight" not in web.get("/migration").text  # preflighting
    assert web.post("/migration/preflight", data={"csrf_token": tok}).status_code == 409
    (job,) = rig.env.jobs(JobKind.MIGRATE_PREFLIGHT)

    out = rig.migrator.config_dir / "migrations" / "1"
    out.mkdir(parents=True)
    (out / "groups.json").write_text(
        json.dumps(
            [
                {"folder": f"/old/{i}", "artist": "a", "album": f"b{i}", "mbid": "", "files": []}
                for i in range(51)
            ]
        )
    )
    with rig.env.sessions.begin() as s:
        m = s.get(Migration, job.payload["migration_id"])
        assert m
        m.manifest_path = str(out / "manifest.jsonl")
        m.summary = {
            "albums": 51,
            "files": 51,
            "bytes": 1,
            "source": "/old",
            "unreadable": [{"path": "A: B/x.mp3", "why": "Permission denied"}],
        }

    def post(**data: str) -> int:
        return web.post("/migration/1/confirm", data={"csrf_token": tok, **data}).status_code

    assert post(paused="1", count="51") == 409  # still preflighting
    with rig.env.sessions.begin() as s:
        m = s.get(Migration, 1)
        assert m
        m.status = "ready"
    html = web.get("/migration").text
    assert "Type 51 to confirm" in html and "I paused other MusicBrainz clients" in html
    assert post(count="51") == 400  # box not ticked
    assert post(paused="1") == 400 and post(paused="1", count="50") == 400
    assert rig.requests() == []
    assert post(paused="1", count="51") == 303
    assert len(rig.requests()) == 51
    assert post(paused="1", count="51") == 303 and len(rig.requests()) == 51  # idempotent
    assert rig.get(1).status == "running"

    r = web.get("/migration/1/report.csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert r.headers["content-type"].startswith("text/csv")
    assert rows[0] == ["folder", "status", "artist", "album", "release_mbid", "files", "reason"]
    assert rows[1][:2] == ["/old/0", "verifying"] and len(rows) == 1 + 51 + 1
    # the unreadable file: a ": " in its path stays in the folder
    assert rows[-1][:2] == ["/old/A: B", "failed"]
    assert rows[-1][-1] == "x.mp3: Permission denied"
    assert "in progress: 51" in web.get("/migration").text
    assert "Check old tree" in web.get("/migration").text
    assert "No requests yet" in web.get("/requests").text  # migration requests are not listed


async def test_retried_preflight_after_confirm_changes_nothing(rig: Rig) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    rig.confirm(mid)
    (job,) = rig.env.jobs(JobKind.MIGRATE_PREFLIGHT)
    groups = Path(rig.get(mid).manifest_path).with_name("groups.json")
    stamp = groups.stat().st_mtime_ns
    await rig.migrator.preflight(rig.env.queue.get(job.id))
    assert rig.get(mid).status == "running" and len(rig.requests()) == 1
    assert groups.stat().st_mtime_ns == stamp
    assert rig.confirm(mid) == 0 and len(rig.requests()) == 1


async def test_a_racing_second_confirm_claims_nothing(rig: Rig) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    with rig.env.sessions() as a, rig.env.sessions() as b:
        ma, mb = a.get(Migration, mid), b.get(Migration, mid)
        assert ma and mb and ma.status == mb.status == "ready"  # both read "ready"
        assert mig.confirm(a, rig.env.clock.now(), ma) == 1
        a.commit()
        assert mig.confirm(b, rig.env.clock.now(), mb) == 0
        b.commit()
    assert len(rig.requests()) == 1 and len(rig.env.jobs(JobKind.VERIFY)) == 1


async def test_csv_cells_cannot_be_formulas(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    rig.confirm(mid)
    with rig.env.sessions.begin() as s:
        req = s.scalars(select(Request)).one()
        req.artist, req.album, req.last_error = "=HYPERLINK(1)", "+1", "@x"
    login(web, "root")
    rows = list(csv.reader(io.StringIO(web.get(f"/migration/{mid}/report.csv").text)))
    assert rows[1][2:4] == ["'=HYPERLINK(1)", "'+1"] and rows[1][6] == "'@x"


async def test_failed_scan_leaves_no_manifest_and_no_check(
    rig: Rig, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "A/One")

    def boom(*_a: Any) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(mig, "scan", boom)
    mid = await rig.preflight()
    m = rig.get(mid)
    assert m.status == "failed" and m.manifest_path == ""
    tok = login(web, "root")
    assert "Check old tree" not in web.get("/migration").text
    assert web.post(f"/migration/{mid}/check", data={"csrf_token": tok}).status_code == 409
    assert rig.env.jobs(JobKind.MIGRATE_CHECK) == []


async def test_only_one_check_runs_at_a_time(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    tok = login(web, "root")
    url = f"/migration/{mid}/check"
    assert web.post(url, data={"csrf_token": tok}).status_code == 303
    assert web.post(url, data={"csrf_token": tok}).status_code == 409
    assert len(rig.env.jobs(JobKind.MIGRATE_CHECK)) == 1


async def test_no_new_preflight_while_unsettled_and_only_the_latest_confirms(
    rig: Rig, web: TestClient
) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    tok = login(web, "root")
    form = {"csrf_token": tok}
    assert web.post("/migration/preflight", data=form).status_code == 409  # ready
    rig.confirm(mid)
    assert web.post("/migration/preflight", data=form).status_code == 409
    await rig.env.drain()  # nothing is found on MusicBrainz: the album waits in review
    assert rig.requests()[0].status == "review"
    assert web.post("/migration/preflight", data=form).status_code == 409
    with rig.env.sessions.begin() as s:
        s.scalars(select(Request)).one().status = "skipped"
    assert web.post("/migration/preflight", data=form).status_code == 303

    with rig.env.sessions.begin() as s:  # the older migration cannot be confirmed any more
        old = s.get(Migration, mid)
        assert old
        old.status = "ready"
    r = web.post(f"/migration/{mid}/confirm", data=form | {"paused": "1"})
    assert r.status_code == 409


async def test_untagged_files_search_by_folder_name_and_keep_the_name(rig: Rig) -> None:
    d = put_album(rig.root, "Some Folder")
    for p in d.glob("*.mp3"):
        tags.write(p, tags.TagSet("", "", "", "", 1, 1))
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    assert rig.meta.searches == [("", "Some Folder", 5)]
    (req,) = rig.requests()
    assert (req.status, req.album) == ("review", "Some Folder")


async def _fail_on(rig: Rig, name: str) -> None:
    real = rig.env.pipeline.fingerprint

    async def fingerprint(path: Path) -> Any:
        if path.name.startswith(name):
            raise PermanentError("fpcalc failed: Invalid data")
        return await real(path)

    rig.env.pipeline = replace(rig.env.pipeline, fingerprint=fingerprint)
    rig.env.handlers = rig.env.pipeline.handlers() | rig.migrator.handlers()


async def test_one_unreadable_track_still_identifies_the_release_and_is_never_imported(
    rig: Rig, web: TestClient
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.meta.refs = [ref(ALBUM)]
    await _fail_on(rig, "02 ")
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert (req.status, d.status, d.release_mbid) == ("review", "pending_review", ALBUM.release_id)
    assert any("02 " in v and "Invalid data" in v for v in d.vetoes)
    tok = login(web, "root")
    assert web.post(f"/review/{d.id}/accept", data={"csrf_token": tok}).status_code == 303
    await rig.env.drain()
    assert len(library(rig.env)) == 1 and not any("02 " in x for x in library(rig.env))


async def test_every_track_unreadable_still_fails(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    rig.meta.refs = [ref(ALBUM)]
    await _fail_on(rig, "0")
    rig.confirm(await rig.preflight())
    await rig.env.drain()

    (req,) = rig.requests()
    assert req.status == "failed" and "fpcalc failed" in (req.last_error or "")
    assert decision_rows(rig.env, 2) == []


async def test_discard_frees_a_ready_preflight_but_not_a_running_migration(
    rig: Rig, web: TestClient
) -> None:
    put_album(rig.root, "A/One")
    mid = await rig.preflight()
    form = {"csrf_token": login(web, "root")}
    assert web.post("/migration/preflight", data=form).status_code == 409  # ready blocks
    assert web.post(f"/migration/{mid}/discard", data=form).status_code == 303
    assert web.post(f"/migration/{mid}/confirm", data={**form, "paused": "1"}).status_code == 409
    assert web.post("/migration/preflight", data=form).status_code == 303
    new = mid + 1
    # still preflighting with a live job: not discardable
    assert web.post(f"/migration/{new}/discard", data=form).status_code == 409
    await rig.env.drain()
    rig.confirm(new)
    assert web.post(f"/migration/{new}/discard", data=form).status_code == 409  # running
    assert rig.requests()


async def test_discard_a_preflight_whose_job_failed(rig: Rig, web: TestClient) -> None:
    form = {"csrf_token": login(web, "root")}
    assert web.post("/migration/preflight", data=form).status_code == 303
    (job,) = rig.env.jobs(JobKind.MIGRATE_PREFLIGHT)
    with rig.env.sessions.begin() as s:
        j = s.get(Job, job.id)
        assert j
        j.status = JobState.FAILED
    mid = job.payload["migration_id"]
    assert web.post(f"/migration/{mid}/discard", data=form).status_code == 303
    assert rig.get(mid).status == "cancelled"
    assert web.post("/migration/preflight", data=form).status_code == 303


def test_scan_lists_what_it_cannot_follow_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "old"
    put_album(root, "A/One")
    (root / "A/One/link.mp3").symlink_to(root / "A/One/01 Song One.mp3")
    (root / "linkdir").symlink_to(root / "A")
    (root / "A/One/gone.mp3").write_text("x")
    locked = root / "locked"
    locked.mkdir()
    (locked / "x.mp3").write_text("x")
    locked.chmod(0)
    real = os.lstat

    def lstat(p: Any, **kw: Any) -> os.stat_result:
        if Path(p).name == "gone.mp3":
            raise FileNotFoundError(2, "No such file or directory")
        return real(p, **kw)

    monkeypatch.setattr(mig.os, "lstat", lstat)
    try:
        found = mig.scan(root, tmp_path / "out")
    finally:
        locked.chmod(0o755)
    notes = sorted(f"{u['path']}: {u['why']}" for u in found["unreadable"])
    assert "A/One/link.mp3: symlink skipped" in notes
    assert "linkdir: symlink skipped" in notes
    assert "A/One/gone.mp3: No such file or directory" in notes
    if os.geteuid() != 0:  # root reads any folder
        assert "locked: cannot read folder" in notes
    manifest = [
        json.loads(x)["path"] for x in (tmp_path / "out/manifest.jsonl").read_text().splitlines()
    ]
    assert sorted(manifest) == [
        "A/One/01 Song One.mp3",
        "A/One/02 Song Two.mp3",
    ]  # nothing followed
    # the check does not crash on an entry that vanishes mid-walk
    result = mig.compare(root, tmp_path / "out/manifest.jsonl")
    assert result["changed"] == [] and result["missing"] == []


async def test_a_tag_that_is_not_a_uuid_goes_straight_to_search(rig: Rig) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid="not-a-uuid")
    rig.meta.refs = [ref(ALBUM)]
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    assert rig.meta.searches == [("Alpha Band", "Alpha Album", 5)]
    assert "not-a-uuid" not in rig.meta.calls


async def test_no_release_offers_no_accept_and_a_failed_album_can_be_retried(
    rig: Rig, web: TestClient
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()
    (d,) = decision_rows(rig.env, 2)
    form = {"csrf_token": login(web, "root")}
    page = web.get(f"/review/{d.id}").text
    assert f'action="/review/{d.id}/accept"' not in page and "No MusicBrainz release" in page
    assert web.post(f"/review/{d.id}/accept", data=form).status_code == 409
    assert decision_rows(rig.env, 2)[0].status == "pending_review"  # still actionable

    (req,) = rig.requests()
    with rig.env.sessions.begin() as s:  # how the album got stuck before the guard
        r = s.get(Request, req.id)
        assert r
        r.status, r.last_error = "failed", "no release was found"
    assert "Retry" in web.get("/migration").text
    url = f"/migration/{mid}/retry/{req.id}"
    assert web.post(url, data=form).status_code == 303
    assert web.post(url, data=form).status_code == 409  # not failed any more: once only
    assert rig.requests()[0].status == "verifying"
    jobs = rig.env.jobs(JobKind.VERIFY)
    assert len(jobs) == 2 and jobs[1].priority == mig.MIGRATION_PRIORITY
    assert jobs[1].payload == {**jobs[0].payload, "release_mbid": None}  # the same files again
    await rig.env.drain()  # matched again: a fresh review item to repick or mark unverified
    assert decision_rows(rig.env, 2)[-1].release_given is False
    assert rig.requests()[0].status == "review"
    assert [x.status for x in decision_rows(rig.env, 2)] == ["pending_review", "pending_review"]
    assert web.post(f"/migration/{mid}/retry/999", data=form).status_code == 404


async def test_extra_files_send_a_migrated_album_to_review_and_are_reported(
    rig: Rig, web: TestClient
) -> None:
    d = put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    shutil.copy(d / "01 Song One.mp3", d / "03 Extra.mp3")  # the release has two tracks
    rig.env.acoustid.override["Alpha Album/03 Extra.mp3"] = [AcoustIdResult("aid-x", 0.95, ("x",))]
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    (dec,) = decision_rows(rig.env, 2)
    assert rig.requests()[0].status == "review" and dec.status == "pending_review"
    assert [Path(x).name for x in dec.evidence["unassigned"]] == ["03 Extra.mp3"]
    tok = login(web, "root")
    assert "03 Extra.mp3" in web.get(f"/review/{dec.id}").text

    assert web.post(f"/review/{dec.id}/accept", data={"csrf_token": tok}).status_code == 303
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.last_error) == (
        "imported",
        "1 file(s) not on the release: 03 Extra.mp3",
    )
    assert len(library(rig.env)) == 2


async def test_retry_of_a_partly_imported_album_imports_the_same_decision_again(
    rig: Rig, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    real = rig.env.ops.import_file

    def flaky(
        src: Path, rel: str, write_tags: Any, batch: str, origin: Path, root: str = "library"
    ) -> str:
        if "Two" in src.name:
            raise OSError("disk full")
        return real(src, rel, write_tags, batch, origin, root)

    monkeypatch.setattr(rig.env.ops, "import_file", flaky)
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()
    (req,) = rig.requests()
    assert req.status == "failed" and len(library(rig.env)) == 1

    monkeypatch.setattr(rig.env.ops, "import_file", real)
    form = {"csrf_token": login(web, "root")}
    assert "Retry" in web.get("/migration").text
    assert web.post(f"/migration/{mid}/retry/{req.id}", data=form).status_code == 303
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.last_error) == ("imported", None) and len(library(rig.env)) == 2
    assert len(decision_rows(rig.env, 2)) == 1  # the same decision, not a new match


async def test_a_skipped_album_can_be_retried(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()
    (d,) = decision_rows(rig.env, 2)
    form = {"csrf_token": login(web, "root")}
    web.post(f"/review/{d.id}/reject", data=form)
    (req,) = rig.requests()
    assert req.status == "skipped" and "Retry" in web.get("/migration").text
    assert web.post(f"/migration/{mid}/retry/{req.id}", data=form).status_code == 303
    assert rig.requests()[0].status == "verifying"


async def test_retrying_a_dead_end_album_clears_the_dead_end(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album")
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()
    (d,) = decision_rows(rig.env, 2)
    form = {"csrf_token": login(web, "root")}
    web.post(f"/review/{d.id}/reject", data=form)
    (req,) = rig.requests()
    with rig.env.sessions.begin() as s:
        s.get_one(Request, req.id).dead_end = True
    assert web.post(f"/migration/{mid}/retry/{req.id}", data=form).status_code == 303
    (req,) = rig.requests()
    assert req.status == "verifying" and req.dead_end is False


async def test_the_page_reports_every_running_migration_and_all_failures(
    rig: Rig, web: TestClient
) -> None:
    put_album(rig.root, "A/One")
    first = await rig.preflight()
    rig.confirm(first)
    await rig.env.drain()
    second = await rig.preflight()
    rig.confirm(second)
    with rig.env.sessions.begin() as s:
        for r in s.scalars(select(Request).where(Request.migration_id == first)):
            r.status, r.last_error = "failed", "first broke"
        for i in range(120):
            s.add(
                Request(
                    user_id=rig.admin,
                    release_mbid="",
                    status="failed",
                    migration_id=second,
                    last_error=f"fail {i}",
                )
            )
    login(web, "root")
    html = web.get("/migration").text
    first_page = html.split(f"<h2>Migration {first}</h2>", 1)[1].split("<h2>Migration ", 1)[0]
    second_page = html.split(f"<h2>Migration {second}</h2>", 1)[1].split("<h2>Migration ", 1)[0]
    assert ">Completed</span>" in first_page and ">Running</span>" in second_page
    assert "first broke" in html and "fail 119" in html


async def test_discard_is_hidden_while_the_preflight_job_is_live(rig: Rig, web: TestClient) -> None:
    form = {"csrf_token": login(web, "root")}
    web.post("/migration/preflight", data=form)
    assert "Discard preflight" not in web.get("/migration").text
    (job,) = rig.env.jobs(JobKind.MIGRATE_PREFLIGHT)
    with rig.env.sessions.begin() as s:
        j = s.get(Job, job.id)
        assert j
        j.status = JobState.FAILED
    assert "Discard preflight" in web.get("/migration").text


async def test_a_migration_album_cannot_be_deleted(rig: Rig, web: TestClient) -> None:
    put_album(rig.root, "A/One")
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    (req,) = rig.requests()
    form = {"csrf_token": login(web, "root")}
    assert web.post(f"/requests/{req.id}/delete", data=form).status_code == 409
    assert len(rig.requests()) == 1


async def test_retry_refuses_when_a_merge_removed_every_imported_file(
    rig: Rig, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    real = rig.env.ops.import_file

    def flaky(
        src: Path, rel: str, write_tags: Any, batch: str, origin: Path, root: str = "library"
    ) -> str:
        if "Two" in src.name:
            raise OSError("disk full")
        return real(src, rel, write_tags, batch, origin, root)

    monkeypatch.setattr(rig.env.ops, "import_file", flaky)
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()
    (req,) = rig.requests()
    with rig.env.sessions.begin() as s:  # what a merge plan leaves: copies journaled, Tracks gone
        for t in s.scalars(select(Track)):
            s.delete(t)
    form = {"csrf_token": login(web, "root")}
    r = web.post(f"/migration/{mid}/retry/{req.id}", data=form)
    assert r.status_code == 409 and "merged or removed" in r.text
    assert rig.requests()[0].status == "failed" and len(rig.env.jobs(JobKind.VERIFY)) == 1


BIG = "44444444-4444-4444-4444-444444444444"


def partial_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """A 13-track release and the one-track album a migration has on disk."""
    tracks = ALBUM.tracks + tuple(
        replace(ALBUM.tracks[0], title=f"Song {i}", index=i, medium_index=i, recording_id=f"r{i}")
        for i in range(3, 14)
    )
    big = replace(ALBUM, release_id=BIG, tracks=tracks)
    monkeypatch.setitem(CATALOG, str(big.release_id), big)
    return big, replace(big, tracks=big.tracks[:1])


async def migrate_partial(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Path:
    big, one = partial_setup(monkeypatch)
    d = put_album(rig.root, "Alpha Band/Alpha Album", one, mbid=str(big.release_id))
    return d


async def run_partial(rig: Rig) -> Any:
    rig.confirm(await rig.preflight())
    await rig.env.drain()
    (dec,) = decision_rows(rig.env, 2)
    return dec


async def test_a_confident_partial_album_imports_and_says_it_is_partial(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    dec = await run_partial(rig)
    assert dec.band == "auto_accept" and dec.evidence["partial"] == "1 of 13 tracks present"
    assert rig.requests()[0].status == "imported" and len(library(rig.env)) == 1
    with rig.env.sessions() as s:
        rows = mig.report(s, s.scalars(select(Migration)).one())
    assert [(r["status"], r["reason"]) for r in rows] == [("partial", "1 of 13 tracks present")]
    assert mig.totals(rows)["partial"] == 1 and mig.totals(rows)["imported"] == 0


def newer_decision(rig: Rig, partial: str | None) -> None:
    """Record a later decision for the migration's request, as a re-verify would."""
    with rig.env.sessions() as s, s.begin():
        old = s.scalars(select(MatchDecision).order_by(MatchDecision.id.desc())).first()
        assert old
        ev = {k: v for k, v in old.evidence.items() if k != "partial"}
        if partial:
            ev["partial"] = partial
        res = MatchResult(0.0, Band.AUTO_ACCEPT, (), (), 1)
        decisions.record(
            s, res, evidence=ev, user_id=old.user_id, stage=2, release_mbid=old.release_mbid
        )


def migration_rows(rig: Rig) -> list[dict[str, Any]]:
    with rig.env.sessions() as s:
        return mig.report(s, s.scalars(select(Migration)).one())


async def test_the_latest_decision_decides_whether_an_import_is_partial(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    await run_partial(rig)
    newer_decision(rig, None)  # a later, complete decision
    rows = migration_rows(rig)
    assert [(r["status"], r["reason"]) for r in rows] == [("imported", "")]
    assert mig.totals(rows)["imported"] == 1 and mig.totals(rows)["partial"] == 0
    newer_decision(rig, "2 of 13 tracks present")  # and a later partial one wins again
    assert [(r["status"], r["reason"]) for r in migration_rows(rig)] == [
        ("partial", "2 of 13 tracks present")
    ]


async def window_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, **window: Any
) -> tuple[Request, list[dict[str, Any]]]:
    await migrate_partial(rig, monkeypatch)
    await run_partial(rig)
    with rig.env.sessions() as s:
        m = s.scalars(select(Migration)).one()
        return rig.requests()[0], mig.report(s, m, **window)


async def test_the_report_window_bounds_rows_by_updated_at(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    req, rows = await window_rows(rig, monkeypatch)  # default window: unchanged
    assert [r["status"] for r in rows] == ["partial"]
    at = req.updated_at.replace(tzinfo=UTC)
    day = timedelta(days=1)
    with rig.env.sessions() as s:
        m = s.scalars(select(Migration)).one()
        assert len(mig.report(s, m, at, at)) == 1  # inclusive at both ends
        assert mig.report(s, m, start=at + day) == []  # started after the album last changed
        assert mig.report(s, m, end=at - day) == []  # ended before it


async def test_a_decision_older_than_the_window_still_sets_the_status(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    await run_partial(rig)
    with rig.env.sessions.begin() as s:
        m = s.scalars(select(Migration)).one()
        at = s.scalars(select(Request)).one().updated_at.replace(tzinfo=UTC)
        s.execute(update(Request).values(updated_at=at + timedelta(days=2)))
        # the album is in the window but its only (partial) decision is not: it is still partial
        assert [r["status"] for r in mig.report(s, m, start=at + timedelta(days=1))] == ["partial"]
        assert [r["status"] for r in mig.report(s, m)] == ["partial"]


async def test_unreadable_files_show_only_when_the_window_reaches_the_start(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    await run_partial(rig)
    with rig.env.sessions.begin() as s:
        m = s.scalars(select(Migration)).one()
        m.summary = {**m.summary, "unreadable": [{"path": "a/b.mp3", "why": "bad"}]}
        began = m.created_at.replace(tzinfo=UTC)
        assert [r["status"] for r in mig.report(s, m)] == ["partial", "failed"]
        assert [r["status"] for r in mig.report(s, m, start=began + timedelta(seconds=1))] == []


async def to_normal_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, other_artist: bool = False, album: str | None = None
) -> Any:
    """The same files and flow as the partial migration, but an ordinary request."""
    await migrate_partial(rig, monkeypatch)
    if album:
        monkeypatch.setitem(CATALOG, BIG, replace(CATALOG[BIG], album=album))
    if other_artist:
        big = CATALOG[BIG]
        monkeypatch.setitem(CATALOG, BIG, replace(big, artist="Zzzz Qqqq", album="Xxxx Wwww"))
    rig.confirm(await rig.preflight())
    with rig.env.sessions.begin() as s:
        s.execute(update(Request).values(migration_id=None, release_mbid=BIG))
        (job,) = s.scalars(select(Job).where(Job.kind == JobKind.VERIFY))
        job.payload = {**job.payload, "release_mbid": BIG}
    await rig.env.drain(until=lambda: bool(decision_rows(rig.env, 2)))
    (dec,) = decision_rows(rig.env, 2)
    return dec


async def test_a_confident_single_track_on_a_normal_request_is_accepted_as_partial(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dec = await to_normal_request(rig, monkeypatch)
    assert dec.band == "auto_accept" and dec.evidence["partial"] == "1 of 13 tracks present"


async def test_an_unsure_single_track_on_a_normal_request_is_rejected_and_retried(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.env.acoustid.override["Alpha Album/01 Song One.mp3"] = [
        AcoustIdResult("aid", 0.5, (RECORDINGS["Song One"],))
    ]
    dec = await to_normal_request(rig, monkeypatch)
    assert dec.band == "reject" and library(rig.env) == []
    assert rig.requests()[0].status == "searching"  # on to the next peer, as for any reject


async def test_a_confident_single_track_whose_album_tag_contradicts_the_release_is_rejected(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dec = await to_normal_request(rig, monkeypatch, album="The Very Best Of Alpha Band")
    assert dec.band == "reject" and not dec.evidence["partial"] and library(rig.env) == []


async def test_a_single_track_of_another_artist_on_a_normal_request_is_rejected(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.env.acoustid.override["Alpha Album/01 Song One.mp3"] = [
        AcoustIdResult("aid", 0.5, (RECORDINGS["Song One"],))
    ]
    dec = await to_normal_request(rig, monkeypatch, other_artist=True)
    assert dec.band == "reject" and library(rig.env) == []


async def test_a_partial_album_with_an_unsure_file_goes_to_review(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    rig.env.acoustid.override["Alpha Album/01 Song One.mp3"] = [
        AcoustIdResult("aid", 0.5, (RECORDINGS["Song One"],))
    ]
    assert (await run_partial(rig)).band == "review"


async def test_a_partial_album_with_a_veto_goes_to_review(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate_partial(rig, monkeypatch)
    big = CATALOG[BIG]
    long_first = replace(big.tracks[0], length=100.0)
    monkeypatch.setitem(
        CATALOG, str(big.release_id), replace(big, tracks=(long_first, *big.tracks[1:]))
    )
    dec = await run_partial(rig)
    assert dec.band == "review" and dec.vetoes
