"""Migration re-import: preflight, the identify-and-import flow, the old-tree check and the page."""

import csv
import hashlib
import io
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire import migration as mig
from mixsync.acquire import pipeline
from mixsync.acquire.migration import Migrator
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Band
from mixsync.core.types import ReleaseRef
from mixsync.db.auth import create_user
from mixsync.db.models.work import Migration, Request
from mixsync.db.queue import enqueue_in
from mixsync.library import tags

from .conftest import Env, login
from .fakes import ALBUM, AUDIO, OTHER, FakeMetadata
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
    assert sorted(u.partition(":")[0] for u in sm["unreadable"]) == [
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


async def test_tagged_album_imports_without_search_and_leaves_the_source_alone(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.root, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    before = rig.tree()

    def boom(*_a: Any) -> None:
        raise AssertionError("the source must not be released")

    monkeypatch.setattr(rig.env.pipeline.importer, "release", boom)
    mid = await rig.preflight()
    rig.confirm(mid)
    await rig.env.drain()

    (req,) = rig.requests()
    assert req.status == "imported" and req.release_mbid == ALBUM.release_id
    assert (req.artist, req.album) == ("Alpha Band", "Alpha Album")
    assert rig.meta.searches == [] and rig.env.source.cleared == []
    assert len(library(rig.env)) == 2
    assert rig.tree() == before  # byte-identical
    assert [d.band for d in decision_rows(rig.env, 2)] == ["auto_accept"]
    assert rig.env.jobs(JobKind.SEARCH) == []


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
    monkeypatch.setattr(
        pipeline, "score_files", lambda *a: replace(real(*a), band=Band.REJECT, distance=0.9)
    )
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
    (verify,) = rig.env.jobs(JobKind.VERIFY)
    assert verify.status is JobState.RETRY_WAIT and decision_rows(rig.env, 2) == []
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
        m.summary = {"albums": 51, "files": 51, "bytes": 1, "source": "/old", "unreadable": ["/x"]}

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
    assert rows[-1][:2] == ["/", "failed"]  # the unreadable file
    assert "in progress: 51" in web.get("/migration").text
    assert "Check old tree" in web.get("/migration").text
    assert "No requests yet" in web.get("/requests").text  # migration requests are not listed
