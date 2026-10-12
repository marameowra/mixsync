"""Discovery retention: keep (star, plays, manual), promote, expire, trash, push."""

import shutil
from datetime import timedelta

from sqlalchemy import select

from mixsync.core.jobs import JobKind, JobState
from mixsync.db import canonical as canonical_db
from mixsync.db.auth import create_user
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import enqueue_in
from mixsync.discovery import retention
from mixsync.discovery.retention import Retention
from mixsync.library import tags

from .conftest import Env
from .fakes import ALBUM, ALBUM_B, AUDIO
from .test_discovery_playlists import DISCOVER_FOLDER, FakeLib, Rig

MBID = ALBUM.tracks[0].recording_id or ""
SONG = "song-1"


class RetLib(FakeLib):
    def __init__(self) -> None:
        super().__init__()
        self.stars: set[str] = set()
        self.plays: dict[str, int] = {}
        self.calls: list[str] = []

    async def starred_song_ids(self) -> set[str]:
        self.calls.append("stars")
        return self.stars

    async def play_counts(
        self, song_ids: list[str], playlist_id: str | None = None
    ) -> dict[str, int]:
        self.calls.append("plays")
        return {i: self.plays.get(i, 0) for i in song_ids}

    async def resolve(
        self, recording_mbid: str, title: str, artist: str, music_folder_id: str | None = None
    ) -> str | None:
        assert music_folder_id == DISCOVER_FOLDER
        return SONG


class RRig(Rig):
    def __init__(self, env: Env) -> None:
        super().__init__(env)
        self.lib = RetLib()
        self.ret = Retention(self.disc, env.pipeline.importer, env.ops, env.metadata)
        with env.sessions.begin() as s:  # the group's canonical release is not the one imported
            canonical_db.store(s, str(ALBUM.release_group_id), str(ALBUM.release_id))

    def hold(self, user_id: int, days: int = 30, **pl: object) -> None:
        """A provisional copy (made once) and the user's hold on it."""
        self.link(user_id)
        pid = self.playlist(user_id, **pl)
        rel = "Alpha Band/x.mp3"
        dst = self.env.data_dir / "discover" / rel
        with self.env.sessions.begin() as s:
            pt = s.scalar(select(ProvisionalTrack).where(ProvisionalTrack.recording_mbid == MBID))
            if pt is None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(AUDIO / "cc0-tone-01.mp3", dst)
                pt = ProvisionalTrack(
                    path=rel,
                    recording_mbid=MBID,
                    release_mbid=ALBUM_B.release_id,
                    release_group_mbid=ALBUM.release_group_id,
                    batch_id="pb",
                    imported_at=self.env.clock.now(),
                )
                s.add(pt)
                s.flush()
            s.add(
                ProvisionalHold(
                    user_id=user_id,
                    recording_mbid=MBID,
                    playlist_id=pid,
                    provisional_track_id=pt.id,
                    expires_at=self.env.clock.now() + timedelta(days=days),
                )
            )
            s.get_one(DiscoveryPlaylist, pid).entries = [
                {"recording_mbid": MBID, "artist": "A", "title": "Song One"}
            ]

    async def retain(self, user_id: int) -> None:
        with self.env.sessions.begin() as s:
            enqueue_in(s, self.env.clock.now(), JobKind.DISCOVERY_RETAIN, {"user_id": user_id})
            j = s.scalars(
                select(Job)
                .where(Job.kind == JobKind.DISCOVERY_RETAIN, Job.status == "queued")
                .order_by(Job.id.desc())
            ).first()
            assert j
            j.status, jid = JobState.SUCCEEDED, j.id
        await self.ret.retain(self.env.queue.get(jid))

    def track(self) -> ProvisionalTrack:
        with self.env.sessions() as s:
            return s.scalars(select(ProvisionalTrack)).one()

    def holds(self) -> list[ProvisionalHold]:
        with self.env.sessions() as s:
            return list(s.scalars(select(ProvisionalHold).order_by(ProvisionalHold.id)))


def library(env: Env) -> list[Track]:
    with env.sessions() as s:
        return list(s.scalars(select(Track)))


def assert_promoted(r: RRig, why: str) -> None:
    (t,) = library(r.env)
    # filed under the canonical release (Alpha), not the one it was imported from (Alpha B)
    assert t.release_mbid == ALBUM.release_id and t.status == "verified"
    assert tags.read_all(r.env.data_dir / "library" / t.path)
    pt = r.track()
    assert pt.status == "promoted" and pt.promoted_path == t.path
    assert pt.trash_path and (r.env.data_dir / ".trash" / pt.trash_path).is_file()
    assert not (r.env.data_dir / "discover" / pt.path).exists()
    assert r.holds()[0].keep_reason == why
    assert r.env.jobs(JobKind.DISCOVERY_PUSH) and r.env.jobs(JobKind.RESCAN)
    with r.env.sessions() as s:  # copy and trash are journaled ops
        assert {o.kind for o in s.scalars(select(FileOp))} >= {"copy", "trash"}


async def test_a_starred_track_is_promoted(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.stars = {SONG}
    await r.retain(env.user_id)
    assert_promoted(r, "star")


async def test_plays_at_the_threshold_promote(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.plays = {SONG: 3}
    await r.retain(env.user_id)
    assert_promoted(r, "plays")
    assert r.lib.calls == ["stars", "plays"]  # once per run


async def test_fewer_plays_are_not_kept(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.plays = {SONG: 2}
    await r.retain(env.user_id)
    assert library(env) == [] and r.track().status == "active"
    assert r.holds()[0].kept_at is None and r.holds()[0].expired_at is None
    assert env.jobs(JobKind.DISCOVERY_PUSH) == []
    assert [j.status for j in env.jobs(JobKind.DISCOVERY_RETAIN)].count("queued") == 1  # the chain


async def test_per_playlist_keep_plays(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id, keep_plays=1)
    r.lib.plays = {SONG: 1}
    await r.retain(env.user_id)
    assert_promoted(r, "plays")


async def test_manual_keep_promotes(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        assert retention.keep(s, env.clock.now(), env.user_id, MBID)
        assert not retention.keep(s, env.clock.now(), env.user_id, "nope")
    await r.retain(env.user_id)
    assert_promoted(r, "manual")


async def test_expiry_trashes_the_file_not_deletes_it(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id, days=30)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)
    pt = r.track()
    assert pt.status == "trashed" and pt.trash_path
    assert (env.data_dir / ".trash" / pt.trash_path).is_file()
    assert not (env.data_dir / "discover" / pt.path).exists()
    assert library(env) == [] and r.holds()[0].expired_at is not None
    with env.sessions() as s:
        trash = [o for o in s.scalars(select(FileOp)) if o.kind == "trash"]
    assert len(trash) == 1 and trash[0].root == "discover"
    assert env.jobs(JobKind.DISCOVERY_PUSH)  # the playlist drops it


async def test_a_shared_track_stays_until_every_user_expires(env: Env) -> None:
    r = RRig(env)
    with env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    r.hold(env.user_id, days=30)
    r.hold(bob, days=60)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)
    pt = r.track()
    assert pt.status == "active" and (env.data_dir / "discover" / pt.path).is_file()
    assert [h.expired_at is not None for h in r.holds()] == [True, False]
    env.clock.advance(timedelta(days=30))
    await r.retain(bob)
    assert r.track().status == "trashed"


async def test_a_track_already_in_the_main_library_is_not_imported_twice(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        s.add(
            Track(
                path="have.mp3",
                recording_mbid=MBID,
                status="verified",
                batch_id="b",
                imported_at=env.clock.now(),
            )
        )
    r.lib.stars = {SONG}
    await r.retain(env.user_id)
    assert [t.path for t in library(env)] == ["have.mp3"]
    assert r.track().status == "promoted" and r.track().promoted_path == "have.mp3"
    assert not list((env.data_dir / "library").rglob("*.mp3"))


async def test_a_user_without_navidrome_is_skipped_with_a_note(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        for link in s.scalars(select(UserLink)):
            s.delete(link)
    env.clock.advance(timedelta(days=40))
    await r.retain(env.user_id)
    assert r.track().status == "active" and r.lib.calls == []
    with env.sessions() as s:
        assert "link" in (s.scalars(select(DiscoveryPlaylist)).one().last_error or "")


async def test_expiring_soon(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id, days=5)
    now = env.clock.now()
    with env.sessions() as s:
        assert len(retention.expiring_soon(s, now, env.user_id)) == 1
        assert retention.expiring_soon(s, now - timedelta(days=3), env.user_id) == []
        assert retention.expiring_soon(s, now, env.user_id + 1) == []


async def test_start_chains_queues_one_run_per_user(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.ret.start_chains()
    r.ret.start_chains()
    assert [j.payload for j in env.jobs(JobKind.DISCOVERY_RETAIN)] == [{"user_id": env.user_id}]


async def test_a_failed_promote_stays_kept_and_the_file_stays(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.stars = {SONG}
    with env.sessions.begin() as s:
        s.scalars(select(ProvisionalTrack)).one().release_mbid = None
    await r.retain(env.user_id)
    pt = r.track()
    assert pt.status == "active" and (env.data_dir / "discover" / pt.path).is_file()
    assert r.holds()[0].kept_at is not None
