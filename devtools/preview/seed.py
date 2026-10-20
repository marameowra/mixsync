"""Fill the preview database through the production helpers: a happy-path set for alice and
admin, and an edge-case set (long and non-Latin text, many rows, failures) for `edge`."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import create_request
from mixsync.core.jobs import JobState
from mixsync.core.matching import Band, MatchResult, Penalty
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.watchlist import FollowHit
from mixsync.db.models.work import Job, Migration, Status
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.db.secretbox import SecretBox, set_secret
from mixsync.watchlist.follows import add_follow

from .fakes import COVER_GROUP, COVER_RELEASE, GROUP_FALLBACK_RELEASE, NO_COVER_RELEASE

PASSWORD = "preview"
USERS = (("admin", True), ("alice", False), ("edge", False))
LONG = "Ünïcödé " + "very long title " * 13  # over 200 characters
NON_LATIN = "夜に駆ける – Ночной дозор – مرحبا بالعالم – Ελληνικά"
G = "11111111-1111-1111-1111-111111111111"
R1, R2 = "11111111-1111-1111-1111-111111111112", "11111111-1111-1111-1111-111111111113"
# request status -> state of its job
REQUEST_STATES: dict[Status, JobState] = {
    "searching": JobState.QUEUED,
    "downloading": JobState.RUNNING,
    "verifying": JobState.RUNNING,
    "importing": JobState.RUNNING,
    "review": JobState.SUCCEEDED,
    "imported": JobState.SUCCEEDED,
    "owned": JobState.SUCCEEDED,
    "no_results": JobState.SUCCEEDED,
    "failed": JobState.FAILED,
    "skipped": JobState.CANCELLED,
}


def _mbid(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(map(str, parts))))


def _request(
    s: Session,
    now: datetime,
    uid: int,
    key: str,
    status: Status,
    artist: str,
    album: str,
    migration_id: int | None = None,
) -> AlbumRequest:
    req = create_request(s, now, uid, _mbid("release", key))
    if key == "alice-imported":
        req.release_mbid, req.release_group_mbid = COVER_RELEASE, COVER_GROUP
    elif key == "alice-failed":
        req.release_mbid = NO_COVER_RELEASE
    req.status, req.artist, req.album, req.migration_id = status, artist, album, migration_id
    if status == "failed":
        req.last_error = "no peer had the files"
    job = s.scalars(select(Job).order_by(Job.id.desc())).first()
    assert job
    job.status = REQUEST_STATES[status]
    return req


def _decision(s: Session, uid: int, title: str, artist: str, files: list[dict[str, str]]) -> int:
    pens = (Penalty("title", 0.5, 2.0, "title differs"), Penalty("year", 0.2, 1.0, "year off"))
    res = MatchResult(0.2, Band.REVIEW, pens, ("v<b>",), 1)
    evidence: dict[str, Any] = {
        "source": {"peer": "peer1", "folder": "Music\\" + title, "files": files},
        "target": {"title": title, "artist": artist, "tracks": []},
        "acoustid": [{"score": 0.97, "recording_ids": ["r1"]}],
        "duration_deltas": [1.5],
    }
    return decisions.record(
        s, res, evidence=evidence, user_id=uid, stage=2, release_mbid=_mbid("decision", title)
    ).id


def _plan_op(kind: str, i: int, name: str = "") -> dict[str, Any]:
    old = {"path": f"a/{name}{i}.flac", "release_mbid": R1}
    if kind == "move":
        tags = {"release_id": R2, "disc": 1, "track": 1, "title": "T", "album": "A"}
        new = f"b/{name}{i}.flac"
        return {"op": kind, "old": old, "new_path": new, "tags": tags, "done": False}
    if kind == "trash":
        return {"op": kind, "old": old, "keeper": f"k/{name}{i}.flac", "done": False}
    return {"op": kind, "old": old, "done": False}


def seed(sf: sessionmaker[Session], box: SecretBox, downloads: Path) -> list[tuple[str, str]]:
    """Returns (label, path) for every seeded page, for the switcher."""
    now = datetime.now(UTC)
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "a.mp3").write_bytes(b"0123456789" * 100)
    links: list[tuple[str, str]] = []
    with sf() as s, s.begin():
        uid = {name: create_user(s, name, PASSWORD, admin=admin).id for name, admin in USERS}
        for service in ("listenbrainz", "navidrome"):
            link = UserLink(
                user_id=uid["alice"],
                service=service,
                external_username="alice",
                secret_encrypted="",
                verified_at=now,
            )
            set_secret(box, link, "secret")
            s.add(link)
        stale = UserLink(  # encrypted with another key: shows the relink state
            user_id=uid["edge"],
            service="navidrome",
            external_username=NON_LATIN,
            secret_encrypted="",
            verified_at=now,
        )
        set_secret(SecretBox(Fernet.generate_key()), stale, "secret")
        s.add(stale)

        ok = [{"local_path": str(downloads / "a.mp3"), "name": "a.mp3", "format": "mp3"}]
        d1 = _decision(s, uid["alice"], "Preview Album", "Preview Artist", ok)
        _decision(s, uid["alice"], "Second Album", "Other Artist", ok)
        d_long = _decision(s, uid["edge"], LONG, NON_LATIN, ok * 3)
        d_none = _decision(s, uid["edge"], "No Evidence Files", "Nobody", [])
        links += [
            ("review list", "/review"),
            ("review detail (alice)", f"/review/{d1}"),
            ("review detail, long/non-Latin (edge)", f"/review/{d_long}"),
            ("review detail, no evidence files (edge)", f"/review/{d_none}"),
        ]

        for status in REQUEST_STATES:
            _request(s, now, uid["alice"], f"alice-{status}", status, "Preview Artist", status)
        _request(s, now, uid["edge"], "edge-long", "imported", NON_LATIN, LONG[:200])
        kinds = list(REQUEST_STATES)
        for i in range(300):
            artist = f"Artist {i} {NON_LATIN}"
            _request(s, now, uid["edge"], f"edge-{i}", kinds[i % len(kinds)], artist, f"Album {i}")
        links.append(("requests (edge has 300)", "/requests"))

        f1 = add_follow(s, now, uid["alice"], "artist", _mbid("artist", 1), "Preview Artist")
        f2 = add_follow(s, now, uid["alice"], "label", _mbid("label", 1), "Preview Records")
        f3 = add_follow(s, now, uid["edge"], "artist", _mbid("artist", 2), LONG[:200])
        for f, title in ((f1, "Release"), (f2, "Release"), (f3, LONG[:200])):
            for i, st in enumerate(
                ("requested", "skipped_owned", "skipped_requested", "skipped_filter", "skipped_cap")
            ):
                s.add(
                    FollowHit(
                        follow_id=f.id,
                        release_group_mbid=COVER_GROUP
                        if f is f1 and i == 0
                        else _mbid("hit", f.id, i),
                        release_mbid=GROUP_FALLBACK_RELEASE
                        if f is f1 and i == 0
                        else _mbid("hit-release", f.id, i),
                        artist=f.name,
                        title=f"{title} {i}",
                        release_date="2020-01-01",
                        status=st,
                    )
                )
        links += [
            ("watchlist", "/watchlist"),
            ("follow, artist (alice)", f"/watchlist/{f1.id}"),
            ("follow, label (alice)", f"/watchlist/{f2.id}"),
            ("catalog (alice)", f"/watchlist/{f1.id}/catalog"),
            ("follow, long name (edge)", f"/watchlist/{f3.id}"),
            ("search", "/watchlist/search?kind=artist&q=x"),
            ("search, empty result", "/watchlist/search?kind=artist&q=none"),
        ]

        # alice's playlist has a track in each state; edge has none
        m = {
            k: _mbid("rec", k)
            for k in ("owned", "waiting", "indiscover", "kept", "expired", "soon")
        }
        pl = DiscoveryPlaylist(
            user_id=uid["alice"],
            name="Discovery",
            last_error="boom",
            entries=[
                {"recording_mbid": v, "artist": "Art", "title": f"T-{k}"} for k, v in m.items()
            ],
        )
        s.add(pl)
        s.flush()
        s.add(
            Track(
                path="o.mp3",
                recording_mbid=m["owned"],
                status="verified",
                batch_id="b",
                imported_at=now,
            )
        )
        for k, days in (("indiscover", 20), ("kept", 20), ("expired", 20), ("soon", 3)):
            pt = ProvisionalTrack(
                path=f"{k}.mp3",
                recording_mbid=m[k],
                batch_id="b",
                imported_at=now,
                title=f"T-{k}",
                artist="Art",
            )
            s.add(pt)
            s.flush()
            s.add(
                ProvisionalHold(
                    user_id=uid["alice"],
                    recording_mbid=m[k],
                    playlist_id=pl.id,
                    provisional_track_id=pt.id,
                    expires_at=now + timedelta(days=days),
                    kept_at=now if k == "kept" else None,
                    expired_at=now if k == "expired" else None,
                )
            )
        s.add(ProvisionalHold(user_id=uid["alice"], recording_mbid=m["waiting"], playlist_id=pl.id))
        links.append(("discover (empty for edge)", "/discover"))

        # two fragments of one release group, and plans in several states
        for i, rel in enumerate((R1, R2)):
            s.add(
                Track(
                    path=f"t{i}.flac",
                    release_mbid=rel,
                    release_group_mbid=G,
                    status="verified",
                    batch_id="b",
                    imported_at=now,
                )
            )
        plans: dict[str, int] = {}
        for status, error, ops in (
            ("draft", None, [_plan_op("move", 0), _plan_op("trash", 1), _plan_op("keep", 2)]),
            ("done", None, [_plan_op("move", 0)]),
            ("failed", "boom <b>", [_plan_op("trash", 1, LONG[:150])]),
            ("discarded", None, [_plan_op("keep", 2)]),
            ("running", None, [_plan_op("move", i) for i in range(150)]),
        ):
            p = Plan(
                kind="merge",
                release_group_mbid=G,
                canonical_release_mbid=R2,
                ops=ops,
                status=status,
                error=error,
            )
            s.add(p)
            s.flush()
            plans[status] = p.id
        links += [("fragments (admin)", "/library/fragments")]
        links += [("unverified (admin)", "/library/unverified")]
        links += [(f"plan, {k} (admin)", f"/library/plans/{v}") for k, v in plans.items()]

        mig = Migration(user_id=uid["admin"], status="running", summary={"albums": 4})
        s.add(mig)
        s.flush()
        for status in ("imported", "failed", "skipped", "searching"):
            _request(s, now, uid["admin"], f"mig-{status}", status, "Old", status, mig.id)
        links.append(("migration (admin)", "/migration"))

    return [("home", "/"), ("account", "/account"), *links]
