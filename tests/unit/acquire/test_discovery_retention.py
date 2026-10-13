"""Discovery retention: keep (star, plays, manual), promote, expire, trash, push."""

import shutil
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import select

from mixsync.acquire.requests import hold_imports
from mixsync.core.clock import aware
from mixsync.core.errors import AuthError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo
from mixsync.core.types import RecordingRef
from mixsync.db import canonical as canonical_db
from mixsync.db.auth import create_user
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import enqueue_in
from mixsync.discovery import retention
from mixsync.discovery.playlists import Discovery, ensure_build
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
        self.song: str | None = SONG  # what resolve finds
        self.gone: set[str] = set()  # ids Navidrome no longer knows
        self.resolved: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.during_stars: Callable[[], None] = lambda: None

    async def starred_song_ids(self) -> set[str]:
        self.calls.append("stars")
        if self.error:
            raise self.error
        self.during_stars()
        return self.stars

    async def play_counts(self, song_ids: list[str]) -> dict[str, int]:
        self.calls.append("plays")
        return {i: self.plays.get(i, 0) for i in song_ids if i not in self.gone}

    async def resolve(
        self, recording_mbid: str, title: str, artist: str, music_folder_id: str | None = None
    ) -> str | None:
        assert music_folder_id == DISCOVER_FOLDER
        self.resolved.append((title, artist))
        return self.song


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


async def test_retention_runs_after_listenbrainz_is_unlinked(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        for link in s.scalars(select(UserLink).where(UserLink.service == "listenbrainz")):
            s.delete(link)
    env.clock.advance(timedelta(days=40))
    await r.retain(env.user_id)
    assert r.holds()[0].expired_at is not None and r.track().status == "trashed"


async def test_retention_does_not_ask_to_relink_a_service_it_does_not_need(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        for link in s.scalars(select(UserLink)):
            if link.service == "navidrome":
                s.delete(link)
            else:
                link.secret_encrypted = "not a token"
    env.clock.advance(timedelta(days=40))
    await r.retain(env.user_id)
    with env.sessions() as s:
        assert "relinking" not in (s.scalars(select(DiscoveryPlaylist)).one().last_error or "")


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


def set_song_id(r: RRig, song_id: str | None) -> None:
    with r.env.sessions.begin() as s:
        s.scalars(select(ProvisionalTrack)).one().navidrome_song_id = song_id


async def test_an_unknown_song_is_never_expired(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.song = None
    env.clock.advance(timedelta(days=40))
    await r.retain(env.user_id)
    pt = r.track()
    assert pt.status == "active" and (env.data_dir / "discover" / pt.path).is_file()
    assert r.holds()[0].expired_at is None
    with env.sessions() as s:
        err = s.scalars(select(DiscoveryPlaylist)).one().last_error or ""
    assert "1 discovery tracks not found in Navidrome" in err
    assert env.jobs(JobKind.DISCOVERY_PUSH) == []


async def test_title_and_artist_come_from_the_track_not_the_playlist(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        s.scalars(select(DiscoveryPlaylist)).one().entries = []  # a newer week's build
        pt = s.scalars(select(ProvisionalTrack)).one()
        pt.title, pt.artist = "Song One", "Alpha Band"
    await r.retain(env.user_id)
    assert r.lib.resolved == [("Song One", "Alpha Band")]


async def test_a_keep_made_during_the_run_wins(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    env.clock.advance(timedelta(days=40))  # due to expire

    def keep_now() -> None:
        with env.sessions.begin() as s:
            assert retention.keep(s, env.clock.now(), env.user_id, MBID)

    r.lib.during_stars = keep_now
    await r.retain(env.user_id)
    assert r.holds()[0].expired_at is None
    assert_promoted(r, "manual")


async def test_trash_never_takes_a_file_with_a_kept_hold(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        h = s.scalars(select(ProvisionalHold)).one()
        h.expired_at = h.kept_at = env.clock.now()
    assert r.ret._trash_unwanted({MBID}) == 0  # pyright: ignore[reportPrivateUsage]
    assert r.track().status == "active"


async def test_a_song_id_navidrome_forgot_is_looked_up_again(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    set_song_id(r, "old")
    r.lib.gone = {"old"}
    r.lib.plays = {SONG: 3}
    await r.retain(env.user_id)
    assert_promoted(r, "plays")
    assert r.track().navidrome_song_id == SONG


async def test_a_forgotten_song_that_cannot_be_found_is_not_expired(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    set_song_id(r, "old")
    r.lib.gone, r.lib.song = {"old"}, None
    env.clock.advance(timedelta(days=40))
    await r.retain(env.user_id)
    assert r.track().status == "active" and r.track().navidrome_song_id is None
    assert r.holds()[0].expired_at is None


async def test_a_trash_failure_after_import_is_finished_by_the_next_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.stars = {SONG}
    real = env.ops.trash
    boom = {"n": 0}

    def flaky(rel: str, batch: str, root: str = "library") -> str:
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("disk went away")
        return real(rel, batch, root)

    monkeypatch.setattr(env.ops, "trash", flaky)
    await r.retain(env.user_id)
    assert r.track().status == "active" and len(library(env)) == 1  # imported, not yet promoted
    assert (env.data_dir / "discover" / r.track().path).is_file()
    await r.retain(env.user_id)
    assert_promoted(r, "star")  # one library row: no second import


@pytest.mark.parametrize("error", [TransientError("down"), AuthError("bad password")])
async def test_a_navidrome_failure_expires_and_trashes_nothing(env: Env, error: Exception) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.error = error
    env.clock.advance(timedelta(days=40))
    if isinstance(error, TransientError):
        with pytest.raises(TransientError):
            await r.retain(env.user_id)
    else:
        await r.retain(env.user_id)
    pt = r.track()
    assert pt.status == "active" and (env.data_dir / "discover" / pt.path).is_file()
    assert r.holds()[0].expired_at is None
    assert [j.status for j in env.jobs(JobKind.DISCOVERY_RETAIN)].count("queued") == 1


async def test_one_user_keeps_while_the_other_expires(env: Env) -> None:
    r = RRig(env)
    with env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    r.hold(env.user_id, days=30)
    r.hold(bob, days=60)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)  # alice's hold expires, bob's still stands
    assert r.track().status == "active" and (env.data_dir / "discover" / r.track().path).is_file()
    r.lib.stars = {SONG}
    await r.retain(bob)
    assert [t.recording_mbid for t in library(env)] == [MBID]
    assert r.track().status == "promoted"
    alice, bobs = r.holds()
    assert alice.kept_at is None and alice.expired_at is not None and bobs.keep_reason == "star"


async def test_a_pick_after_expiry_is_retained_again(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id, days=30)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)
    assert r.track().status == "trashed" and r.holds()[0].expired_at is not None
    with env.sessions.begin() as s:  # a later build picks it again, and it is imported again
        pl = s.scalars(select(DiscoveryPlaylist)).one()
        hold = Discovery._hold(s, env.clock.now(), pl, MBID, download=True)  # pyright: ignore[reportPrivateUsage]
        assert hold.expired_at is None and hold.provisional_track_id is None
        assert hold.expires_at is None and hold.kept_at is None  # the import starts the clock
        s.add(
            ProvisionalTrack(
                path="Alpha Band/y.mp3",
                recording_mbid=MBID,
                release_mbid=ALBUM_B.release_id,
                release_group_mbid=ALBUM.release_group_id,
                batch_id="pb2",
                imported_at=env.clock.now(),
            )
        )
        s.flush()
        hold_imports(s, env.clock.now(), "pb2")
    shutil.copy(AUDIO / "cc0-tone-01.mp3", env.data_dir / "discover" / "Alpha Band/y.mp3")
    with env.sessions() as s:
        assert (
            len(retention.expiring_soon(s, env.clock.now(), env.user_id, timedelta(days=31))) == 1
        )
    r.lib.stars = {SONG}
    await r.retain(env.user_id)
    assert [t.recording_mbid for t in library(env)] == [MBID]


async def test_a_slow_redownload_after_a_repick_gets_its_window_from_the_import(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id, days=30)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)
    with env.sessions.begin() as s:
        pl = s.scalars(select(DiscoveryPlaylist)).one()
        Discovery._hold(s, env.clock.now(), pl, MBID, download=True)  # pyright: ignore[reportPrivateUsage]
    env.clock.advance(timedelta(days=35))  # the download takes longer than the retention
    with env.sessions.begin() as s:
        s.add(
            ProvisionalTrack(
                path="Alpha Band/y.mp3",
                recording_mbid=MBID,
                release_mbid=ALBUM_B.release_id,
                release_group_mbid=ALBUM.release_group_id,
                batch_id="pb2",
                imported_at=env.clock.now(),
            )
        )
        s.flush()
        hold_imports(s, env.clock.now(), "pb2")
    shutil.copy(AUDIO / "cc0-tone-01.mp3", env.data_dir / "discover" / "Alpha Band/y.mp3")
    await r.retain(env.user_id)
    hold = r.holds()[0]
    assert hold.expired_at is None
    assert aware(hold.expires_at) == env.clock.now() + timedelta(days=30)


async def test_a_repick_on_a_track_another_user_keeps_gets_a_fresh_window(env: Env) -> None:
    r = RRig(env)
    with env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    r.hold(env.user_id, days=30)
    r.hold(bob, days=60)
    env.clock.advance(timedelta(days=31))
    await r.retain(env.user_id)  # alice expires; bob's hold keeps the track active
    assert r.track().status == "active" and r.holds()[0].expired_at is not None
    r.history.weekly = [RecordingRef(MBID, ALBUM_B.release_id, "A", "Song One")]
    with env.sessions.begin() as s:
        ensure_build(s, env.clock.now(), s.scalars(select(DiscoveryPlaylist)).first())  # type: ignore[arg-type]
    await r.run(JobKind.DISCOVERY_BUILD)
    alice = r.holds()[0]
    assert alice.expired_at is None and alice.provisional_track_id == r.track().id
    assert aware(alice.expires_at) == env.clock.now() + timedelta(days=30)  # clock from the pick
    await r.retain(env.user_id)
    assert r.holds()[0].expired_at is None and r.track().status == "active"


async def test_a_build_error_survives_unknown_songs_and_the_note_clears(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    with env.sessions.begin() as s:
        s.scalars(select(DiscoveryPlaylist)).one().last_error = "ListenBrainz rejected the token"

    def error() -> str | None:
        with env.sessions() as s:
            return s.scalars(select(DiscoveryPlaylist)).one().last_error

    r.lib.song = None
    await r.retain(env.user_id)
    assert error() == "ListenBrainz rejected the token"
    with env.sessions.begin() as s:
        s.scalars(select(DiscoveryPlaylist)).one().last_error = None
    await r.retain(env.user_id)
    assert "not found in Navidrome" in (error() or "")
    await r.retain(env.user_id)  # the note is ours: it is rewritten, not stacked
    assert (error() or "").count("not found in Navidrome") == 1
    r.lib.song = SONG
    await r.retain(env.user_id)
    assert error() is None


async def test_a_recording_imported_meanwhile_is_not_imported_again(env: Env) -> None:
    r = RRig(env)
    r.hold(env.user_id)
    r.lib.stars = {SONG}
    real = env.metadata.get_release

    async def racing(mbid: str) -> AlbumInfo:
        with env.sessions.begin() as s:  # another import lands while we await MusicBrainz
            if s.scalar(select(Track.id).where(Track.recording_mbid == MBID)) is None:
                s.add(
                    Track(
                        path="raced.mp3",
                        recording_mbid=MBID,
                        status="verified",
                        batch_id="other",
                        imported_at=env.clock.now(),
                    )
                )
        return await real(mbid)

    env.metadata.get_release = racing  # type: ignore[method-assign]
    await r.retain(env.user_id)
    assert [t.path for t in library(env)] == ["raced.mp3"]
    assert r.track().status == "promoted" and r.track().promoted_path == "raced.mp3"
