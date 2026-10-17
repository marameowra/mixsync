"""The discovery generator: ratio, ownership, requests and holds, the Navidrome push, the chain."""

from datetime import UTC, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire import lifecycle
from mixsync.acquire.requests import hold_imports
from mixsync.core.clock import aware
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import RecordingRef
from mixsync.db.auth import create_user
from mixsync.db.engine import user_sessions
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import USER_PRIORITY, JobRecord, enqueue_in
from mixsync.db.secretbox import SecretBox, set_secret
from mixsync.discovery import playlists
from mixsync.discovery.playlists import NO_LINKS, Discovery

from .conftest import Env, login

NAME = "Discovery"


def rec(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def ref(n: int, release: str | None = "11111111-0000-0000-0000-000000000001") -> RecordingRef:
    return RecordingRef(rec(n), release, "Artist", f"Title {n}")


class Mid:
    def __call__(self, a: float, b: float) -> float:
        return (a + b) / 2


class FakeHistory:
    def __init__(self) -> None:
        self.weekly: list[RecordingRef] = []
        self.recs: list[RecordingRef] = []
        self.meta: dict[str, RecordingRef] = {}
        self.calls: list[str] = []

    async def weekly_exploration(self, user: str, token: str) -> list[RecordingRef]:
        self.calls.append("weekly")
        return self.weekly

    async def recommendations(self, user: str, token: str, count: int) -> list[RecordingRef]:
        self.calls.append("recs")
        return self.recs

    async def lb_radio(self, prompt: str, mode: str, token: str) -> list[RecordingRef]:
        self.calls.append("radio")
        return []

    async def recording_metadata(self, mbids: list[str]) -> dict[str, RecordingRef]:
        self.calls.append("meta")
        return {m: self.meta[m] for m in mbids if m in self.meta}

    async def validate_token(self, token: str) -> str | None:
        return "u"


MAIN_FOLDER, DISCOVER_FOLDER = "1", "7"


class FakeLib:
    """Navidrome as the push sees it: two libraries; unrestricted searches see discover first."""

    def __init__(self) -> None:
        self.main: dict[str, str] = {}  # recording -> song id, in the main library
        self.disc: dict[str, str] = {}  # the same, in the discover library
        self.folders: dict[str, str] = {"Music Library": MAIN_FOLDER, "discover": DISCOVER_FOLDER}
        self.error: Exception | None = None
        self.playlists: dict[str, list[str]] = {}
        self.names: dict[str, str] = {}
        self.credentials: list[tuple[str, str]] = []

    async def push_playlist(self, name: str, song_ids: list[str], existing_id: str | None) -> str:
        pid = existing_id if existing_id in self.playlists else f"pl{len(self.playlists) + 1}"
        self.playlists[pid], self.names[pid] = song_ids, name
        return pid

    async def starred_song_ids(self) -> set[str]:
        return set()

    async def play_counts(self, song_ids: list[str]) -> dict[str, int]:
        return {}

    async def music_folder_id(self, name: str) -> str | None:
        return self.folders.get(name)

    async def resolve(
        self, recording_mbid: str, title: str, artist: str, music_folder_id: str | None = None
    ) -> str | None:
        if self.error:
            raise self.error
        if music_folder_id == MAIN_FOLDER:
            return self.main.get(recording_mbid)
        if music_folder_id == DISCOVER_FOLDER:
            return self.disc.get(recording_mbid)
        return self.disc.get(recording_mbid) or self.main.get(recording_mbid)


class Rig:
    def __init__(self, env: Env) -> None:
        self.env, self.history, self.lib = env, FakeHistory(), FakeLib()
        self.box = SecretBox(Fernet.generate_key())
        self.disc = Discovery(
            env.sessions,
            env.clock,
            self.history,
            self._lib,
            self.box,
            Mid(),
        )

    def _lib(self, user: str, password: str) -> FakeLib:
        self.lib.credentials.append((user, password))
        return self.lib

    def link(self, user_id: int, services: tuple[str, ...] = ("listenbrainz", "navidrome")) -> None:
        with self.env.sessions.begin() as s:
            for svc in services:
                link = UserLink(
                    user_id=user_id,
                    service=svc,
                    external_username=f"{svc}-name",
                    verified_at=self.env.clock.now(),
                )
                set_secret(self.box, link, f"{svc}-secret")
                s.add(link)

    def playlist(self, user_id: int | None = None, **kw: object) -> int:
        with self.env.sessions.begin() as s:
            pl = DiscoveryPlaylist(
                user_id=user_id or self.env.user_id, name=NAME, sources=playlists.DEFAULT_SOURCES
            )
            for k, v in kw.items():
                setattr(pl, k, v)
            s.add(pl)
            s.flush()
            return pl.id

    async def run(self, kind: JobKind) -> int:
        """Run the oldest queued job of `kind`, as the worker would; returns its id."""
        with self.env.sessions.begin() as s:
            j = s.scalars(
                select(Job).where(Job.kind == kind, Job.status == "queued").order_by(Job.id)
            ).first()
            assert j
            j.status, jid = JobState.SUCCEEDED, j.id
        handler = self.disc.handlers()[kind]
        await handler(self.env.queue.get(jid))
        return jid

    def pl(self, pid: int) -> DiscoveryPlaylist:
        with self.env.sessions() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            assert pl
            s.expunge(pl)
            return pl


def own(env: Env, *ns: int) -> None:
    with env.sessions.begin() as s:
        for n in ns:
            s.add(
                Track(
                    path=f"t{n}.mp3",
                    recording_mbid=rec(n),
                    status="verified",
                    batch_id="b",
                    imported_at=env.clock.now(),
                )
            )


def provisional(env: Env, n: int, status: str = "active", batch: str = "pb") -> int:
    with env.sessions.begin() as s:
        pt = ProvisionalTrack(
            path=f"p{n}-{status}.mp3",
            recording_mbid=rec(n),
            batch_id=batch,
            imported_at=env.clock.now(),
            status=status,
        )
        s.add(pt)
        s.flush()
        return pt.id


def requests(env: Env) -> list[Request]:
    with env.sessions() as s:
        return list(s.scalars(select(Request).order_by(Request.id)))


def holds(env: Env) -> list[ProvisionalHold]:
    with env.sessions() as s:
        return list(s.scalars(select(ProvisionalHold).order_by(ProvisionalHold.id)))


@pytest.fixture
def rig(env: Env) -> Rig:
    return Rig(env)


async def test_ratio_is_respected_and_the_rest_is_owned_without_duplicates(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, *range(1, 41))  # 1-40 owned
    rig.history.weekly = [ref(n) for n in range(1, 81)] + [ref(3)]  # 41-80 unowned, a repeat
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    entries = rig.pl(pid).entries
    mbids = [e["recording_mbid"] for e in entries]
    assert len(mbids) == len(set(mbids)) == 50
    unowned = [m for m in mbids if int(m[-12:]) > 40]
    assert len(unowned) == 15 and unowned == [rec(n) for n in range(41, 56)]  # source order
    assert [m for m in mbids if int(m[-12:]) <= 40] == [rec(n) for n in range(1, 36)]
    assert len(requests(rig.env)) == 1 and len(holds(rig.env)) == 15  # one release
    r = requests(rig.env)[0]
    assert (r.source, r.provisional, r.recording_mbids) == (
        "discovery",
        True,
        [rec(n) for n in range(41, 56)],
    )
    assert rig.history.calls == ["weekly"]  # long enough: no top-up
    assert rig.pl(pid).last_error is None


async def test_short_weekly_exploration_is_topped_up_from_recommendations(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(size=4)
    own(rig.env, 1, 2, 3)
    rig.history.weekly = [ref(1)]
    rig.history.recs = [ref(2), ref(3), ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert rig.history.calls == ["weekly", "recs"]
    assert [e["recording_mbid"] for e in rig.pl(pid).entries] == [rec(1), rec(2), rec(3)]


async def test_owned_means_library_or_active_provisional_and_users_hold_them(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(retention_days=10)
    own(rig.env, 1)
    ptid = provisional(rig.env, 2)
    provisional(rig.env, 3, status="trashed")  # a trashed copy is not owned
    rig.history.weekly = [ref(1), ref(2), ref(3)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert [r.recording_mbids for r in requests(rig.env)] == [[rec(3)]]
    by = {h.recording_mbid: h for h in holds(rig.env)}
    assert set(by) == {rec(2), rec(3)}  # none for a main-library track
    assert by[rec(2)].provisional_track_id == ptid and by[rec(2)].request_id is None
    assert by[rec(2)].expires_at is not None
    assert by[rec(3)].expires_at is None and by[rec(3)].playlist_id == pid


async def test_a_fill_hold_on_an_old_import_starts_its_clock_now(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist(retention_days=10)
    ptid = provisional(rig.env, 2)
    with rig.env.sessions.begin() as s:  # imported 40 days ago for another user
        s.get_one(ProvisionalTrack, ptid).imported_at = rig.env.clock.now() - timedelta(days=40)
    rig.history.weekly = [ref(2)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    (hold,) = holds(rig.env)
    assert aware(hold.expires_at) == rig.env.clock.now() + timedelta(days=10)


async def test_two_users_one_request_and_two_holds(rig: Rig) -> None:
    with rig.env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    for uid in (rig.env.user_id, bob):
        rig.link(uid)
        rig.playlist(uid)
    rig.history.weekly = [ref(1)]
    rig.history.recs = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert len(requests(rig.env)) == 1
    hs = holds(rig.env)
    assert {h.user_id for h in hs} == {rig.env.user_id, bob}
    assert {h.request_id for h in hs} == {requests(rig.env)[0].id}


async def test_a_live_request_is_not_duplicated_but_a_failed_one_is_retried(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    with rig.env.sessions.begin() as s:
        s.add(
            Request(
                user_id=rig.env.user_id,
                release_mbid="r",
                provisional=True,
                recording_mbids=[rec(1)],
                status="downloading",
            )
        )
        s.add(
            Request(
                user_id=rig.env.user_id,
                release_mbid="r",
                provisional=True,
                recording_mbids=[rec(2)],
                status="failed",
            )
        )
    rig.history.weekly = [ref(1), ref(2)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rs = requests(rig.env)
    assert [r.recording_mbids for r in rs] == [[rec(1)], [rec(2)], [rec(2)]]
    assert {h.recording_mbid: h.request_id for h in holds(rig.env)} == {
        rec(1): rs[0].id,
        rec(2): rs[2].id,
    }
    assert rig.pl(pid).entries


async def test_missing_release_is_looked_up_or_skipped(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    rig.history.weekly = [ref(1, None), ref(2, None)]
    rig.history.meta = {rec(1): ref(1, "22222222-0000-0000-0000-000000000002")}
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert rig.history.calls.count("meta") == 1  # one batched lookup
    rs = requests(rig.env)
    assert [(r.recording_mbids, r.release_mbid) for r in rs] == [
        ([rec(1)], "22222222-0000-0000-0000-000000000002")
    ]
    assert [h.recording_mbid for h in holds(rig.env)] == [rec(1)]


async def test_empty_listenbrainz_pushes_nothing_and_says_so(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    pl = rig.pl(pid)
    assert pl.entries == [] and pl.last_error and "no tracks" in pl.last_error
    assert pl.last_built_at is not None
    assert not rig.env.jobs(JobKind.DISCOVERY_PUSH) and not rig.lib.playlists


async def test_push_replaces_contents_recreates_and_restricts_provisional(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, 1)
    provisional(rig.env, 2)
    rig.lib.main, rig.lib.disc = {rec(1): "s1"}, {rec(2): "s2"}
    rig.history.weekly = [ref(1), ref(2), ref(3)]  # 3 is not imported yet
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert (
        rig.lib.playlists == {"pl1": ["s1", "s2"]} and rig.lib.names["pl1"] == "MixSync: Discovery"
    )
    assert rig.pl(pid).navidrome_playlist_id == "pl1"
    assert rig.lib.credentials[-1] == ("navidrome-name", "navidrome-secret")
    rig.lib.disc = {}  # song 2 went away: contents are replaced, not appended
    await rig.disc.push(_record(rig, pid))
    assert rig.lib.playlists == {"pl1": ["s1"]}
    rig.lib.playlists.clear()  # the user deleted it in Navidrome
    await rig.disc.push(_record(rig, pid))
    assert rig.lib.playlists == {"pl1": ["s1"]} and rig.pl(pid).navidrome_playlist_id == "pl1"


def _record(rig: Rig, pid: int) -> JobRecord:
    """A fresh push job for the playlist (the hook's or a build's), as a record to run."""
    with rig.env.sessions.begin() as s:
        enqueue_in(s, rig.env.clock.now(), JobKind.DISCOVERY_PUSH, {"playlist_id": pid})
        jid = s.scalars(select(Job.id).order_by(Job.id.desc())).first()
        assert jid
    return rig.env.queue.get(jid)


async def test_an_unscanned_song_is_skipped_then_pushed_after_the_import_hook(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(retention_days=10)
    own(rig.env, 1)
    rig.lib.main = {rec(1): "s1"}
    rig.history.weekly = [ref(1), ref(2)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert rig.lib.playlists["pl1"] == ["s1"]  # 2 is requested, not imported
    provisional(rig.env, 2, batch="batch-2")  # the pipeline imported it...
    with rig.env.sessions.begin() as s:
        hold_imports(s, rig.env.clock.now(), "batch-2")
        hold_imports(s, rig.env.clock.now(), "batch-2")  # a re-run adds no second push
    h = holds(rig.env)[0]
    assert h.provisional_track_id and h.expires_at
    assert h.expires_at.replace(tzinfo=UTC) == rig.env.clock.now() + timedelta(days=10)
    pushes = [j for j in rig.env.jobs(JobKind.DISCOVERY_PUSH) if j.status == "queued"]
    assert len(pushes) == 2 and {j.payload["playlist_id"] for j in pushes} == {pid}  # early, late
    await rig.run(JobKind.DISCOVERY_PUSH)  # ...but Navidrome has not scanned it
    assert rig.lib.playlists["pl1"] == ["s1"]
    rig.lib.disc[rec(2)] = "s2"  # the rescan ran
    with rig.env.sessions.begin() as s:
        hold_imports(s, rig.env.clock.now(), "batch-2")  # same key: still one push waiting
    await rig.disc.push(_record(rig, pid))
    assert rig.lib.playlists["pl1"] == ["s1", "s2"]


async def test_unfound_discover_library_searches_unrestricted(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    provisional(rig.env, 2)
    rig.lib.disc, rig.lib.folders = {rec(2): "s2"}, {}
    rig.history.weekly = [ref(2)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert rig.lib.playlists == {"pl1": ["s2"]}
    assert "'discover' library is not visible" in (rig.pl(pid).last_error or "")


async def test_next_build_is_one_jittered_job_and_a_rerun_adds_none(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(cadence_days=7)
    rig.disc.start_chains()
    rig.disc.start_chains()  # a second start adds nothing
    assert len(rig.env.jobs(JobKind.DISCOVERY_BUILD)) == 1
    jid = await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.disc.build(rig.env.queue.get(jid))  # a re-run of the same job
    nxt = [j for j in rig.env.jobs(JobKind.DISCOVERY_BUILD) if j.status == "queued"]
    assert len(nxt) == 1
    assert nxt[0].run_after.replace(tzinfo=UTC) == rig.env.clock.now() + timedelta(days=7)
    assert rig.pl(pid).next_build_at


@pytest.mark.parametrize(("cadence", "edge"), [(7, timedelta(hours=6)), (1, timedelta(hours=2.4))])
@pytest.mark.parametrize("sign", [-1, 1])
async def test_jitter_is_a_tenth_of_the_cadence_and_at_most_six_hours(
    rig: Rig, cadence: int, edge: timedelta, sign: int
) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(cadence_days=cadence)
    rig.disc.rng = lambda a, b: sign * b  # the end of the range asked for
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    due = rig.pl(pid).next_build_at
    want = rig.env.clock.now() + timedelta(days=cadence) + sign * edge
    assert due and due.replace(tzinfo=UTC) == want


async def test_a_failing_source_still_queues_the_next_build(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()

    async def boom(user: str, token: str) -> list[RecordingRef]:
        raise PermanentError("down")

    rig.history.weekly_exploration = boom  # type: ignore[method-assign]
    rig.disc.start_chains()
    with pytest.raises(PermanentError):
        await rig.run(JobKind.DISCOVERY_BUILD)
    assert any(j.status == "queued" for j in rig.env.jobs(JobKind.DISCOVERY_BUILD))


async def test_a_user_without_links_is_skipped_with_a_clear_error(rig: Rig) -> None:
    rig.link(rig.env.user_id, ("listenbrainz",))  # no Navidrome
    pid = rig.playlist()
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert rig.pl(pid).last_error == NO_LINKS
    assert not rig.history.calls and not requests(rig.env)
    assert any(j.status == "queued" for j in rig.env.jobs(JobKind.DISCOVERY_BUILD))  # chain goes on


async def test_the_default_playlist_is_made_once_for_a_listenbrainz_user(rig: Rig) -> None:
    with rig.env.sessions.begin() as s:
        create_user(s, "bob", "pw")  # no link: no playlist
    rig.link(rig.env.user_id)
    rig.disc.start_chains()
    rig.disc.start_chains()
    with rig.env.sessions() as s:
        pls = list(s.scalars(select(DiscoveryPlaylist)))
    assert [(p.user_id, p.size, p.unowned_ratio, p.cadence_days) for p in pls] == [
        (rig.env.user_id, 50, 0.3, 7)
    ]
    assert (pls[0].retention_days, pls[0].keep_plays) == (30, 3)


def test_build_now_queues_at_user_priority_and_only_for_the_owner(
    rig: Rig, web: TestClient
) -> None:
    env = rig.env
    with env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    rig.link(env.user_id)
    pid = rig.playlist()
    with env.sessions.begin() as s:
        pl = s.get(DiscoveryPlaylist, pid)
        assert pl
        playlists.ensure_build(s, env.clock.now(), pl)
        for j in s.scalars(select(Job)):  # the weekly build is days away
            j.run_after = env.clock.now() + timedelta(days=3)
            j.priority = 0
        enqueue_in(s, env.clock.now(), JobKind.SEARCH, {})
    env.clock.advance(timedelta(hours=1))  # the web request stamps real time
    tok = login(web, "bob")
    assert web.post(f"/discover/{pid}/build", data={"csrf_token": tok}).status_code == 403
    tok = login(web, "alice")
    assert pid and "Discovery" in web.get("/discover").text
    assert web.post(f"/discover/{pid}/build", data={"csrf_token": tok}).status_code == 303
    first = env.queue.claim("w")
    assert first and first.kind == JobKind.DISCOVERY_BUILD
    with env.sessions() as s:
        assert s.get(Job, first.id).priority == USER_PRIORITY  # type: ignore[union-attr]
    assert web.post("/discover/999/build", data={"csrf_token": tok}).status_code == 404


def test_build_now_is_refused_within_the_hour_and_the_page_only_reads(
    rig: Rig, web: TestClient
) -> None:
    env = rig.env
    rig.link(env.user_id)
    tok = login(web, "alice")
    assert "No playlists yet" in web.get("/discover").text  # a visit creates nothing
    with env.sessions() as s:
        assert s.scalars(select(DiscoveryPlaylist)).first() is None
    pid = rig.playlist()
    page = web.get("/discover").text
    assert "never" in page and "Build now" in page
    with env.sessions.begin() as s:
        pl = s.get(DiscoveryPlaylist, pid)
        assert pl
        pl.last_built_at = env.clock.now()
    with user_sessions(env.sessions).begin() as s:
        pl = s.get(DiscoveryPlaylist, pid)
        assert pl and not playlists.build_now(s, env.clock.now(), pl)
    assert web.post(f"/discover/{pid}/build", data={"csrf_token": tok}).status_code == 409


REL_A = "11111111-0000-0000-0000-00000000000a"
REL_B = "11111111-0000-0000-0000-00000000000b"


def test_two_concurrent_default_creations_end_with_one_playlist(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.link(rig.env.user_id)
    # both callers saw "no playlist yet": the unique (user, name) key settles the race
    monkeypatch.setattr(playlists, "_has_playlist", lambda s, uid: False)
    for _ in range(2):
        with rig.env.sessions.begin() as s:
            playlists.ensure_default(s, rig.env.clock.now(), rig.env.user_id)
    with rig.env.sessions() as s:
        assert len(list(s.scalars(select(DiscoveryPlaylist)))) == 1
    assert len(rig.env.jobs(JobKind.DISCOVERY_BUILD)) == 1


async def test_picks_on_one_release_make_one_request(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    rig.history.weekly = [ref(1, REL_A), ref(2, REL_A), ref(3, REL_B)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rs = requests(rig.env)
    assert [(r.release_mbid, r.recording_mbids) for r in rs] == [
        (REL_A, [rec(1), rec(2)]),
        (REL_B, [rec(3)]),
    ]
    assert {h.recording_mbid: h.request_id for h in holds(rig.env)} == {
        rec(1): rs[0].id,
        rec(2): rs[0].id,
        rec(3): rs[1].id,
    }


async def test_the_import_hook_alone_repushes_once_navidrome_has_scanned(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    own(rig.env, 1)
    rig.lib.main = {rec(1): "s1"}
    rig.history.weekly = [ref(1), ref(2)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    provisional(rig.env, 2, batch="batch-2")
    with rig.env.sessions.begin() as s:
        hold_imports(s, rig.env.clock.now(), "batch-2")
    early, late = (j for j in rig.env.jobs(JobKind.DISCOVERY_PUSH) if j.status == "queued")
    assert late.run_after > early.run_after
    await rig.run(JobKind.DISCOVERY_PUSH)  # the scan is still running
    assert rig.lib.playlists["pl1"] == ["s1"]
    rig.lib.disc[rec(2)] = "s2"  # ...and has finished by the later push
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert rig.lib.playlists["pl1"] == ["s1", "s2"]


async def test_a_main_library_track_resolves_to_the_main_copy(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    own(rig.env, 1)
    provisional(rig.env, 1)  # both copies exist
    rig.lib.main, rig.lib.disc = {rec(1): "main"}, {rec(1): "disc"}
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert rig.lib.playlists == {"pl1": ["main"]}


async def test_an_undecryptable_link_says_to_relink(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    rig.disc.box = SecretBox(Fernet.generate_key())  # the key changed
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    err = rig.pl(pid).last_error or ""
    assert err.endswith("needs relinking on /account")
    assert err.split()[0] in ("ListenBrainz", "Navidrome")
    assert not rig.history.calls


async def test_a_rejected_listenbrainz_token_is_recorded_not_retried(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()

    async def refused(user: str, token: str) -> list[RecordingRef]:
        raise AuthError("ListenBrainz rejected the user token")

    rig.history.weekly_exploration = refused  # type: ignore[method-assign]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)  # does not raise
    assert "rejected the token" in (rig.pl(pid).last_error or "")
    assert any(j.status == "queued" for j in rig.env.jobs(JobKind.DISCOVERY_BUILD))


async def test_navidrome_refusing_the_account_is_recorded_not_retried(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, 1)
    rig.lib.main = {rec(1): "s1"}
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rig.lib.error = AuthError("Navidrome rejected the credentials")
    await rig.run(JobKind.DISCOVERY_PUSH)  # does not raise
    assert (rig.pl(pid).last_error or "").startswith("Navidrome: Navidrome rejected")


async def test_a_recording_known_not_to_be_on_the_release_is_not_asked_for_again(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    with rig.env.sessions.begin() as s:
        req = Request(
            user_id=rig.env.user_id,
            release_mbid=REL_A,
            provisional=True,
            recording_mbids=[rec(1)],
        )
        s.add(req)
        lifecycle.dead_end(req, "none of the wanted recordings are on the downloaded release")
    rig.history.weekly = [ref(1, REL_A), ref(2, REL_A), ref(1, REL_B)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rs = requests(rig.env)[1:]
    assert [(r.release_mbid, r.recording_mbids) for r in rs] == [(REL_A, [rec(2)])]
    assert [e["recording_mbid"] for e in rig.pl(pid).entries] == [rec(2)]


async def test_picks_without_a_release_do_not_take_a_slot(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist(size=10)  # 3 unowned slots
    rig.history.weekly = [ref(1, None), ref(2, None)] + [ref(n) for n in range(3, 9)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert [e["recording_mbid"] for e in rig.pl(pid).entries] == [rec(3), rec(4), rec(5)]


async def test_a_good_push_clears_an_earlier_push_error(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, 1)
    rig.lib.main = {rec(1): "s1"}
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rig.lib.error = AuthError("Navidrome rejected the credentials")
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert (rig.pl(pid).last_error or "").startswith("Navidrome: ")
    rig.lib.error = None  # relinked
    await rig.disc.push(_record(rig, pid))
    assert rig.pl(pid).last_error is None and rig.lib.playlists == {"pl1": ["s1"]}


async def test_unfound_main_library_is_noted(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, 1)
    rig.lib.main, rig.lib.folders = {rec(1): "s1"}, {}
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_PUSH)
    assert "'Music Library' library is not visible" in (rig.pl(pid).last_error or "")


async def test_a_recording_missing_from_an_imported_release_is_not_asked_for_again(
    rig: Rig,
) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    with rig.env.sessions.begin() as s:
        req = Request(
            user_id=rig.env.user_id,
            release_mbid=REL_A,
            provisional=True,
            recording_mbids=[rec(1), rec(2)],
        )
        s.add(req)
        lifecycle.finished(req, [], ["1 of 2 wanted recordings not on this release"], dead_end=True)
    provisional(rig.env, 2)  # the one it did bring in
    rig.history.weekly = [ref(1, REL_A), ref(3, REL_A)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rs = requests(rig.env)[1:]
    assert [(r.release_mbid, r.recording_mbids) for r in rs] == [(REL_A, [rec(3)])]


async def test_only_dead_pairs_for_the_asked_releases_block_a_request(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    rig.playlist()
    with rig.env.sessions.begin() as s:
        for rel, mbids in ((REL_A, [rec(1)]), (REL_B, [rec(2), rec(3)])):
            req = Request(
                user_id=rig.env.user_id,
                release_mbid=rel,
                provisional=True,
                recording_mbids=mbids,
            )
            s.add(req)
            lifecycle.dead_end(req, "none of the wanted recordings are on this release")
    # (REL_A, rec1) and (REL_B, rec2) are dead; the same recordings on another release are not
    rig.history.weekly = [ref(1, REL_A), ref(2, REL_A), ref(3, REL_A), ref(2, REL_B)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    rs = requests(rig.env)[2:]
    assert [(r.release_mbid, r.recording_mbids) for r in rs] == [(REL_A, [rec(2), rec(3)])]


async def test_a_good_push_keeps_a_build_error(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    pid = rig.playlist()
    own(rig.env, 1)
    rig.lib.main = {rec(1): "s1"}
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)  # entries now exist

    async def refused(user: str, token: str) -> list[RecordingRef]:
        raise AuthError("ListenBrainz rejected the user token")

    rig.history.weekly_exploration = refused  # type: ignore[method-assign]
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert "rejected the token" in (rig.pl(pid).last_error or "")
    await rig.disc.push(_record(rig, pid))  # an import-triggered push
    assert "rejected the token" in (rig.pl(pid).last_error or "")
    assert rig.lib.playlists == {"pl1": ["s1"]}
