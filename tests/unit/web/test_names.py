import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import create_request
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.matching import Band, MatchResult
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.library import Plan, Track
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient

G = "11111111-1111-1111-1111-111111111111"
R1 = "11111111-1111-1111-1111-111111111112"
R2 = "11111111-1111-1111-1111-111111111113"
REC = "11111111-1111-1111-1111-111111111114"
ART = "11111111-1111-1111-1111-111111111115"
CREDIT = [{"name": "Band", "joinphrase": "", "artist": {"id": ART}}]
BODIES = {
    "release": {
        "id": R1,
        "title": "Album",
        "date": "2019-05-01",
        "artist-credit": CREDIT,
        "media": [{"format": "CD"}],
    },
    "release-group": {
        "id": G,
        "title": "Group",
        "first-release-date": "2018",
        "artist-credit": CREDIT,
    },
    "recording": {"id": REC, "title": "Song", "artist-credit": CREDIT},
    "artist": {"id": ART, "name": "Band"},
}
GROUP_LABEL = ">Group — Band (2018)</a>"


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def upstream() -> dict[str, Any]:
    """What the fake MusicBrainz answers; a test changes it to break the upstream."""
    return {"status": 200, "content": None}


@pytest.fixture
def mb(
    sf: sessionmaker[Session],
    make_client: Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient],
    calls: list[str],
    upstream: dict[str, Any],
) -> MusicBrainzProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if upstream["status"] != 200 or upstream["content"] is not None:
            return httpx.Response(upstream["status"], content=upstream["content"] or b"")
        return httpx.Response(200, json=BODIES[request.url.path.split("/")[-2]])

    return MusicBrainzProvider(make_client("musicbrainz", handler))


async def test_label_per_kind_and_cache_only(mb: MusicBrainzProvider, calls: list[str]) -> None:
    assert await mb.label("release", R1, cached_only=True) is None
    assert calls == []  # a cache-only miss never goes upstream
    assert await mb.label("release", R1) == "Album — Band (2019, CD)"
    assert await mb.label("release-group", G) == "Group — Band (2018)"
    assert await mb.label("recording", REC) == "Song — Band"
    assert await mb.label("artist", ART) == "Band"
    n = len(calls)
    assert await mb.label("release", R1, cached_only=True) == "Album — Band (2019, CD)"
    assert len(calls) == n


@pytest.fixture
def client(sf: sessionmaker[Session], mb: MusicBrainzProvider) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "root", "pw", admin=True)
        for i, rel in enumerate((R1, R2)):  # two releases in one group: fragmented
            s.add(
                Track(
                    path=f"t{i}.flac",
                    release_mbid=rel,
                    release_group_mbid=G,
                    status="verified",
                    batch_id="b",
                    imported_at=datetime.now(UTC),
                )
            )
        s.add(Plan(kind="merge", release_group_mbid=G, canonical_release_mbid=R2, ops=[]))
    app = create_app(sf, Settings())
    app.state.namer = mb
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"username": "root", "password": "pw"})
        yield c


def test_list_page_with_unknown_ids_makes_no_upstream_call(
    client: TestClient, calls: list[str]
) -> None:
    html = client.get("/library/fragments").text
    assert f'title="{G}">Release group 11111111</a>' in html  # the fallback, id on hover
    assert f"https://musicbrainz.org/release-group/{G}" in html
    assert calls == []


def test_detail_page_resolves_once_then_the_list_uses_the_cache(
    client: TestClient, calls: list[str], sf: sessionmaker[Session]
) -> None:
    with sf() as s:
        pid = s.query(Plan).one().id
    assert GROUP_LABEL in client.get(f"/library/plans/{pid}").text
    assert 0 < len(calls) <= 3
    n = len(calls)
    client.get(f"/library/plans/{pid}")
    assert len(calls) == n  # the second render is served from the cache
    assert GROUP_LABEL in client.get("/library/fragments").text
    assert len(calls) == n


def test_local_decision_names_a_release_without_upstream(
    client: TestClient, sf: sessionmaker[Session], calls: list[str]
) -> None:
    with sf() as s, s.begin():
        uid = s.query(User).one().id
        d = decisions.record(
            s,
            MatchResult(0.2, Band.REVIEW, (), (), 1),
            evidence={"source": {"files": []}, "target": {"title": "Local", "artist": "Me"}},
            user_id=uid,
            stage=2,
            release_mbid=R1,
        )
        did = d.id
    assert f'title="{R1}">Local — Me</a>' in client.get(f"/review/{did}").text
    assert calls == []


def _decision(sf: sessionmaker[Session], **evidence: Any) -> int:
    with sf() as s, s.begin():
        uid = s.query(User).one().id
        return decisions.record(
            s,
            MatchResult(0.2, Band.REVIEW, (), (), 1),
            evidence={"source": {"files": []}, **evidence},
            user_id=uid,
            stage=2,
            release_mbid=R1,
        ).id


def test_release_cached_by_get_release_names_the_list_page(
    client: TestClient, mb: MusicBrainzProvider, calls: list[str]
) -> None:
    asyncio.run(mb.get_release(R1))  # what the pipeline caches
    n = len(calls)
    assert ">Album \u2014 Band (2019, CD)</a>" in client.get("/library/fragments").text
    assert len(calls) == n


@pytest.mark.parametrize(
    ("status", "content"), [(404, b""), (500, b""), (200, b"not json"), (200, b'{"x": 1}')]
)
def test_broken_upstream_falls_back_to_short_ids(
    client: TestClient,
    sf: sessionmaker[Session],
    upstream: dict[str, Any],
    status: int,
    content: bytes,
) -> None:
    upstream.update(status=status, content=content)
    with sf() as s:
        pid = s.query(Plan).one().id
    r = client.get(f"/library/plans/{pid}")
    assert r.status_code == 200
    assert f'title="{G}">Release group 11111111</a>' in r.text


def test_detail_page_makes_at_most_three_lookups(
    client: TestClient, sf: sessionmaker[Session], calls: list[str]
) -> None:
    ops = [
        {
            "op": "move",
            "old": {"path": f"{i}.flac", "release_mbid": f"22222222-2222-2222-2222-22222222222{i}"},
            "new_path": f"n{i}.flac",
            "tags": {"release_id": R2, "title": "T"},
            "done": False,
        }
        for i in range(4)
    ]
    with sf() as s, s.begin():
        plan = s.query(Plan).one()
        plan.ops = ops
        pid = plan.id
    assert client.get(f"/library/plans/{pid}").status_code == 200
    assert len(calls) == 3


def test_request_without_album_is_named_locally_or_from_cache(
    client: TestClient, sf: sessionmaker[Session], mb: MusicBrainzProvider, calls: list[str]
) -> None:
    _decision(sf, target={"title": "Local", "artist": "Me"})
    with sf() as s, s.begin():
        uid = s.query(User).one().id
        create_request(s, datetime.now(UTC), uid, R1)
        create_request(s, datetime.now(UTC), uid, R2)
    asyncio.run(mb.label("release", R2))
    n = len(calls)
    html = client.get("/requests").text
    assert f'title="{R1}">Local \u2014 Me</a>' in html
    assert f'title="{R2}">' in html and "Release 11111111" not in html
    assert len(calls) == n


def test_non_uuid_ref_is_never_looked_up(
    client: TestClient, sf: sessionmaker[Session], calls: list[str]
) -> None:
    did = _decision(sf, target={"title": "T", "artist": "A"}, acoustid=[{"recording_ids": ["r1"]}])
    assert 'title="r1">Recording r1</a>' in client.get(f"/review/{did}").text
    assert calls == []
