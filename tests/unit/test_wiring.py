from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync import wiring, worker
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind
from mixsync.library import tag_undo
from mixsync.library.fileops import FileOps
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.web import deps
from mixsync.wiring import Services, build_services


class Counted(MusicBrainzProvider):
    made = 0

    def __init__(self, *a: Any, **k: Any) -> None:
        Counted.made += 1
        super().__init__(*a, **k)


def _owner(handlers: dict[JobKind, Any], kind: JobKind) -> Any:
    return handlers[kind].__self__


def test_a_worker_graph_has_one_musicbrainz_provider(
    settings: Settings, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    Counted.made = 0
    monkeypatch.setattr(wiring, "MusicBrainzProvider", Counted)
    services = build_services(settings, sessions)
    _, _, handlers, _ = worker._new_queue(services)  # pyright: ignore[reportPrivateUsage]
    mb = services.musicbrainz
    assert Counted.made == 1
    assert _owner(handlers, JobKind.MERGE).metadata is mb
    assert _owner(handlers, JobKind.WATCHLIST_POLL).watch is mb
    assert _owner(handlers, JobKind.SEARCH).metadata is mb
    assert _owner(handlers, JobKind.RECHECK).metadata is mb
    assert _owner(handlers, JobKind.DISCOVERY_RETAIN).metadata is mb
    assert _owner(handlers, JobKind.MERGE) is services.merger


def test_a_worker_graph_shares_one_of_each_client(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    services = build_services(settings, sessions)
    _, _, handlers, _ = worker._new_queue(services)  # pyright: ignore[reportPrivateUsage]
    pipeline = _owner(handlers, JobKind.SEARCH)
    assert pipeline.coverart is services.coverart
    assert pipeline.importer.fileops is services.fileops
    assert _owner(handlers, JobKind.DISCOVERY_BUILD).box is services.secret_box
    assert _owner(handlers, JobKind.ACOUSTID_SUBMIT).acoustid is pipeline.acoustid


def test_web_resolves_merger_watch_and_namer_to_one_provider(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    app = create_app(sessions, settings)
    request: Any = type("R", (), {"app": app})()
    services: Services = app.state.services
    assert deps.get_merger(request).metadata is services.musicbrainz
    assert deps.get_watch(request) is services.musicbrainz
    assert deps.get_namer(request) is services.musicbrainz


def test_build_services_runs_no_recovery(
    settings: Settings, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object) -> None:
        pytest.fail("recovery ran while building services")

    monkeypatch.setattr(FileOps, "recover", boom)
    monkeypatch.setattr(tag_undo, "recover", boom)
    build_services(settings, sessions)


async def test_serve_all_hands_one_services_to_the_worker_and_the_app(
    settings: Settings, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, Any] = {}

    def new_queue(services: Services) -> tuple[None, str, dict[JobKind, Any], tuple[()]]:
        seen["worker"] = services
        return None, "w", {}, ()

    class Config:
        def __init__(self, app: Any, **_k: Any) -> None:
            self.app = app

    class Server:
        should_exit = False

        def __init__(self, config: Config) -> None:
            self.config = config

        async def serve(self) -> None:
            seen["app"] = self.config.app().state.services

    async def idle(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(worker, "_new_queue", new_queue)
    monkeypatch.setattr(worker, "run_worker", idle)
    monkeypatch.setattr(uvicorn, "Config", Config)
    monkeypatch.setattr(uvicorn, "Server", Server)
    await worker.serve_all()
    assert isinstance(seen["worker"], Services)
    assert seen["app"] is seen["worker"]
